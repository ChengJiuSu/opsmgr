"""飞书出站客户端：tenant_access_token 缓存 + 统一请求封装 + 消息接口。

所有出站调用都从这里走，不散在各处：

- **token 必须缓存**。`tenant_access_token` 有效期 2 小时，每次发卡片现取一遍
  等于把出站请求数翻倍，还会撞上飞书的取 token 限频。缓存放共享缓存层（生产
  是 Redis），WSGI / celery 多进程共用一份。
- **token 失效要能自愈**。管理员在飞书后台重置了 App Secret、或缓存里的 token
  被提前作废时，飞书返回 99991663 一类的错误码；此时清缓存、重取一次、重试
  一次请求。不自愈的话表现是「接下来两小时飞书全挂」。
- **失败一律抛 FeishuError**。调用方（异步发卡任务）只记日志，绝不能把飞书的
  抖动回写成任务执行结果——见 apps/im_mgr/tasks.py（T9）。

超时取 settings.FEISHU_TIMEOUT：卡片回调要求 3 秒内回包，读超时定长了会把
回调拖死，所以真正的发卡都走异步任务，这里的超时只是兜底。
"""
import json
import logging

import requests
from django.conf import settings
from django.core.cache import cache

from .models import FeishuApp

logger = logging.getLogger(__name__)

TOKEN_CACHE_KEY = 'feishu:tenant_access_token'
#: 提前这么多秒判定过期，避免拿着一张「还有 3 秒到期」的 token 去发请求
TOKEN_SAFETY_MARGIN = 300
#: 飞书返回这些错误码 = token 不可用，清缓存重取一次
INVALID_TOKEN_CODES = (99991661, 99991663, 99991664, 99991668)

TOKEN_PATH = '/open-apis/auth/v3/tenant_access_token/internal'
MESSAGE_PATH = '/open-apis/im/v1/messages'
USER_PATH = '/open-apis/contact/v3/users'


class FeishuError(Exception):
    """飞书接口调用失败（网络异常或业务错误码）。"""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def _app():
    """取已启用且配置齐全的飞书应用配置。"""
    app = FeishuApp.load()
    if not app.is_ready():
        missing = app.missing_fields()
        reason = f'缺少配置：{"、".join(missing)}' if missing else '未启用飞书接入'
        raise FeishuError(f'飞书接入不可用（{reason}）')
    return app


def _session():
    session = requests.Session()
    # 飞书在公网：是否吃环境代理由部署决定（见 settings.FEISHU_TRUST_ENV）
    session.trust_env = getattr(settings, 'FEISHU_TRUST_ENV', True)
    return session


def _url(path):
    return getattr(settings, 'FEISHU_BASE_URL', 'https://open.feishu.cn').rstrip('/') + path


def _request(session, method, path, *, headers=None, payload=None, params=None):
    """发一次请求并解出飞书的 {code, msg, data} 信封。

    网络异常与非 JSON 响应都转成 FeishuError：调用方只需要 catch 一种异常。
    """
    try:
        resp = session.request(
            method, _url(path), headers=headers, json=payload, params=params,
            timeout=getattr(settings, 'FEISHU_TIMEOUT', (5, 10)),
        )
    except requests.RequestException as exc:
        raise FeishuError(f'请求飞书接口失败：{exc}') from exc
    try:
        body = resp.json()
    except ValueError as exc:
        raise FeishuError(
            f'飞书接口返回非 JSON（HTTP {resp.status_code}）：{resp.text[:200]}') from exc
    if not isinstance(body, dict):
        raise FeishuError(f'飞书接口返回结构异常：{str(body)[:200]}')
    return body


def get_tenant_token(app=None, force_refresh=False):
    """取 tenant_access_token，默认读缓存。

    缓存不可用时退化为每次现取（fail-open）：这里失败的后果只是多打几次接口，
    与 nonce 去重那种「放行即可被重放」的判定不同，不需要 fail-closed。
    """
    if not force_refresh:
        try:
            cached = cache.get(TOKEN_CACHE_KEY)
        except Exception:  # noqa: BLE001 - 缓存后端异常类型由实现决定
            logger.warning('飞书 token 缓存不可用，改为现取', exc_info=True)
            cached = None
        if cached:
            return cached

    app = app or _app()
    body = _request(
        _session(), 'POST', TOKEN_PATH,
        payload={'app_id': app.app_id, 'app_secret': app.app_secret},
    )
    if body.get('code'):
        raise FeishuError(
            f'获取 tenant_access_token 失败：{body.get("msg")}（code={body.get("code")}）',
            code=body.get('code'),
        )
    token = body.get('tenant_access_token')
    if not token:
        raise FeishuError('飞书未返回 tenant_access_token')
    ttl = int(body.get('expire') or 7200) - TOKEN_SAFETY_MARGIN
    if ttl > 0:
        try:
            cache.set(TOKEN_CACHE_KEY, token, timeout=ttl)
        except Exception:  # noqa: BLE001
            logger.warning('飞书 token 写缓存失败，本次照常使用', exc_info=True)
    return token


def drop_token_cache():
    try:
        cache.delete(TOKEN_CACHE_KEY)
    except Exception:  # noqa: BLE001
        logger.warning('飞书 token 缓存清理失败', exc_info=True)


def call(method, path, *, payload=None, params=None, app=None):
    """调一次飞书业务接口，返回 data 部分；业务错误码抛 FeishuError。

    token 失效（INVALID_TOKEN_CODES）时自动清缓存、重取、重试一次。只重试
    一次：真的是 App Secret 被改了的话，重试多少次都一样，快点报错更有用。
    """
    app = app or _app()
    session = _session()
    for attempt in (1, 2):
        token = get_tenant_token(app, force_refresh=attempt == 2)
        body = _request(
            session, method, path, payload=payload, params=params,
            headers={'Authorization': f'Bearer {token}'},
        )
        code = body.get('code')
        if not code:
            return body.get('data') or {}
        if code in INVALID_TOKEN_CODES and attempt == 1:
            logger.warning('飞书 token 失效（code=%s），清缓存重试一次', code)
            drop_token_cache()
            continue
        raise FeishuError(
            f'飞书接口 {path} 调用失败：{body.get("msg")}（code={code}）', code=code)
    raise FeishuError(f'飞书接口 {path} 重试后仍失败')


# ── 消息与卡片 ────────────────────────────────────────────

def send_card(receive_id, card, receive_id_type='chat_id', app=None):
    """发一张交互卡片，返回 message_id（更新卡片时要用，务必落库）。"""
    data = call(
        'POST', MESSAGE_PATH,
        params={'receive_id_type': receive_id_type},
        payload={
            'receive_id': receive_id,
            'msg_type': 'interactive',
            # content 必须是 JSON **字符串**，不是对象——传对象飞书报参数非法
            'content': json.dumps(card, ensure_ascii=False),
        },
        app=app,
    )
    return data.get('message_id') or ''


def update_card(message_id, card, app=None):
    """整体替换一条卡片消息的内容（执行进度刷新走这条）。"""
    if not message_id:
        raise FeishuError('缺少 message_id，无法更新卡片')
    return call(
        'PATCH', f'{MESSAGE_PATH}/{message_id}',
        payload={'content': json.dumps(card, ensure_ascii=False)},
        app=app,
    )


def send_text(receive_id, text, receive_id_type='chat_id', app=None):
    """发纯文本，用于「卡片渲染不出来」这类兜底提示。"""
    data = call(
        'POST', MESSAGE_PATH,
        params={'receive_id_type': receive_id_type},
        payload={
            'receive_id': receive_id,
            'msg_type': 'text',
            'content': json.dumps({'text': text}, ensure_ascii=False),
        },
        app=app,
    )
    return data.get('message_id') or ''


# ── 通讯录 ────────────────────────────────────────────────

def get_user(open_id, app=None):
    """按 open_id 取用户信息（name / email / union_id），供 SSO 绑定用。"""
    data = call(
        'GET', f'{USER_PATH}/{open_id}',
        params={'user_id_type': 'open_id'}, app=app,
    )
    return data.get('user') or {}
