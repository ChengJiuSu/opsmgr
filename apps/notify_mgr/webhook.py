"""飞书自定义群机器人（Webhook）出站客户端。

与应用机器人（apps/im_mgr/client.py）的区别：不用 tenant_access_token，一个 token
就能发；但**只能发、不能改、按钮不能回调**——所以群机器人只发终态结果卡，
「重试 / 跳过」这类交互仍只走应用机器人。

几条规矩：

- **地址由 token 拼出来**（FEISHU_BASE_URL + 固定路径），不接受任意 URL：后台
  能填 URL 就等于能让服务器去请求任意内网地址（SSRF）；
- **签名**：机器人开了签名校验时，`sign = base64(hmac_sha256(key=f"{ts}\\n{secret}", msg=b""))`，
  注意 key 是拼出来的串、消息体为空——飞书文档的写法，反过来算签名不对；
- **限流**：单个机器人 5 次/秒、100 次/分钟。超了飞书回错误码，这里先按秒做
  本地节拍，撞上飞书限流再退避重试一次；
- **失败一律抛 WebhookError**，调用方只记录不外溢。
"""
import base64
import hashlib
import hmac
import logging
import re
import time

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

HOOK_PATH = '/open-apis/bot/v2/hook/'
#: token 的形状（飞书给的是 UUID）。宽一点放行，但不能含 / ? # 这类会改写路径的字符
TOKEN_RE = re.compile(r'^[A-Za-z0-9_-]{8,128}$')
#: 从整条 Webhook 地址里抠 token（管理员多半直接粘贴整条地址）
URL_TOKEN_RE = re.compile(r'/open-apis/bot/v2/hook/([A-Za-z0-9_-]{8,128})/?$')
#: 飞书限流类错误码：退避后重试一次
RATE_LIMIT_CODES = (9499, 11232, 11233)
#: 本地节拍：每个机器人每秒最多发这么多条（飞书上限 5）
PER_SECOND = 4
RATE_PREFIX = 'notify:webhook:rate:'


class WebhookError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def normalize_token(value):
    """接受 token 或整条 Webhook 地址，返回 token；不合法返回 None。"""
    value = str(value or '').strip()
    if not value:
        return None
    if '/' in value:
        matched = URL_TOKEN_RE.search(value.split('?')[0])
        return matched.group(1) if matched else None
    return value if TOKEN_RE.match(value) else None


def hook_url(token):
    base = getattr(settings, 'FEISHU_BASE_URL', 'https://open.feishu.cn').rstrip('/')
    return f'{base}{HOOK_PATH}{token}'


def sign(timestamp, secret):
    """飞书自定义机器人签名。"""
    key = f'{timestamp}\n{secret}'.encode('utf-8')
    digest = hmac.new(key, b'', digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode('utf-8')


def _pace(robot_id):
    """本地节拍：同一秒内超过 PER_SECOND 条就等到下一秒。

    缓存不可用时直接放行（fail-open）：多发几条最坏撞上飞书限流，那里还有一次
    退避重试兜着；因为缓存挂了把通知卡住才是更糟的结果。
    """
    for _ in range(3):
        bucket = f'{RATE_PREFIX}{robot_id}:{int(time.time())}'
        try:
            cache.add(bucket, 0, timeout=2)
            count = cache.incr(bucket)
        except Exception:  # noqa: BLE001
            return
        if count <= PER_SECOND:
            return
        time.sleep(1.0 - (time.time() % 1.0) + 0.01)


def _post(url, payload):
    session = requests.Session()
    session.trust_env = getattr(settings, 'FEISHU_TRUST_ENV', True)
    try:
        resp = session.post(url, json=payload,
                            timeout=getattr(settings, 'FEISHU_TIMEOUT', (5, 10)))
    except requests.RequestException as exc:
        raise WebhookError(f'请求飞书群机器人失败：{exc}') from exc
    try:
        body = resp.json()
    except ValueError as exc:
        raise WebhookError(
            f'飞书群机器人返回非 JSON（HTTP {resp.status_code}）：{resp.text[:200]}') from exc
    if not isinstance(body, dict):
        raise WebhookError(f'飞书群机器人返回结构异常：{str(body)[:200]}')
    return body


def _error_code(body):
    # 新版信封是 {code, msg, data}，老版是 {StatusCode, StatusMessage}
    code = body.get('code')
    if code is None:
        code = body.get('StatusCode')
    return code or 0


def send(robot, payload):
    """往群机器人发一条消息（payload 是 msg_type + content/card）。"""
    token = robot.hook_token
    if not token or not TOKEN_RE.match(token):
        raise WebhookError('群机器人 Webhook token 无效')
    payload = dict(payload)
    if robot.secret:
        timestamp = str(int(time.time()))
        payload.update(timestamp=timestamp, sign=sign(timestamp, robot.secret))

    url = hook_url(token)
    for attempt in (1, 2):
        _pace(robot.pk)
        body = _post(url, payload)
        code = _error_code(body)
        if not code:
            return body
        if code in RATE_LIMIT_CODES and attempt == 1:
            logger.warning('飞书群机器人限流（code=%s），退避后重试一次', code)
            time.sleep(1.5)
            continue
        message = body.get('msg') or body.get('StatusMessage') or str(body)[:200]
        raise WebhookError(f'飞书群机器人发送失败：{message}（code={code}）', code=code)
    raise WebhookError('飞书群机器人重试后仍失败')


def send_card(robot, card):
    """发一张卡片。群机器人的卡片放在 `card` 字段里，是对象而不是 JSON 串。"""
    return send(robot, {'msg_type': 'interactive', 'card': card})
