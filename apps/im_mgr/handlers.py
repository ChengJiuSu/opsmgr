"""飞书回调的业务路由（事件 / 卡片动作）。

与 views.py 的分工：视图只负责「这条请求可不可信」（来源 IP、验签、解密、幂等）
与「怎么回包」，业务落在这里。分开是为了让那道门保持薄——门上的每一行改动都
直接影响公网入口的安全性，不该跟卡片交互逻辑混在一起。

交互形态（刻意不做 `/task run xxx` 那种命令行）：

    @bot 任意消息  →  任务选择卡（下拉）
    选中一条任务   →  确认卡（范围 / 模块数 / 主机数）
    点「执行」     →  非生产直接发起；生产再出一张二次确认卡（一次性票据）

命令行要记任务名、易打错、也没地方塞二次确认；卡片把「看清楚再点」做成了流程
本身的一部分。

三条贯穿始终的规矩：

- **鉴权按点按钮的人判**，不是按发卡片的人。群里的卡片谁都看得见、按钮谁都
  点得到，所以每个动作都先把 `open_id` 解析成平台 User，解析不到直接拒；
- **卡片上的值只用于展示**。任务 id 之外的一切（主机范围、模块、权限）都在
  动作发生时按库里的当前值重新解析——卡片可能是十分钟前发的；
- **3 秒内回包**。按钮动作全部在响应体里直接回新卡片，不发出站请求；需要主动
  发卡的路径（事件）走 tasks.py 异步出去。

payload 来自公网，即便验签通过也只说明「确实是飞书转发的」，内容仍是群里的人
打的字：取值一律带默认、不做格式化拼接。
"""
import json
import logging

from django.conf import settings

from apps.task_exec import trigger
from apps.task_exec.models import ExecutionTask

from . import access, cards, notify, tasks
from .models import FeishuBinding, FeishuConfirmToken

logger = logging.getLogger(__name__)

#: 事件订阅：v2 事件类型在 header.event_type
EVENT_MESSAGE_RECEIVE = 'im.message.receive_v1'
#: 生产二次确认票据的有效期（秒）
CONFIRM_TTL = 60
#: 审计里记的路径：**不带 callback_slug**——审计的读权限比系统配置宽，
#: 把回调地址的随机串写进去等于把那层共享密钥降级保管
AUDIT_PATH = '/api/feishu/callback/card/'


# ── payload 解析（兼容 v1 / v2 两种形态）──────────────────

def event_type(payload):
    header = payload.get('header')
    if isinstance(header, dict):
        return str(header.get('event_type') or '')
    event = payload.get('event')
    if isinstance(event, dict):
        return str(event.get('type') or '')
    return ''


def event_id(payload):
    header = payload.get('header')
    if isinstance(header, dict):
        return str(header.get('event_id') or '')
    return str(payload.get('uuid') or '')


def _event(payload):
    event = payload.get('event')
    return event if isinstance(event, dict) else {}


def operator_ids(payload):
    """点这次按钮 / 发这条消息的人，返回 (open_id, union_id)。"""
    event = _event(payload)
    for holder in (event.get('operator'), (event.get('sender') or {}).get('sender_id')):
        if isinstance(holder, dict) and (holder.get('open_id') or holder.get('union_id')):
            return str(holder.get('open_id') or ''), str(holder.get('union_id') or '')
    # v1 卡片回调把 open_id 放在顶层
    return str(payload.get('open_id') or ''), str(payload.get('union_id') or '')


def context_ids(payload):
    """这次交互发生在哪，返回 (chat_id, message_id)。"""
    event = _event(payload)
    context = event.get('context')
    if isinstance(context, dict):
        return (str(context.get('open_chat_id') or ''),
                str(context.get('open_message_id') or ''))
    message = event.get('message')
    if isinstance(message, dict):
        return str(message.get('chat_id') or ''), str(message.get('message_id') or '')
    return (str(payload.get('open_chat_id') or ''),
            str(payload.get('open_message_id') or ''))


def action_payload(payload):
    """按钮/下拉的回传体，返回 (value dict, 下拉选中值)。"""
    action = _event(payload).get('action') or payload.get('action') or {}
    if not isinstance(action, dict):
        return {}, ''
    value = action.get('value')
    if isinstance(value, str):
        # 部分客户端会把 value 序列化成字符串回传
        try:
            value = json.loads(value)
        except ValueError:
            value = {}
    return (value if isinstance(value, dict) else {}), str(action.get('option') or '')


def message_text(payload):
    """取消息正文，剥掉 @机器人 的占位符。"""
    message = _event(payload).get('message') or {}
    content = message.get('content')
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except ValueError:
            return ''
    if not isinstance(content, dict):
        return ''
    import re

    return re.sub(r'@_user_\d+', '', str(content.get('text') or '')).strip()


def resolve_user(payload):
    """点按钮的人 → 平台 User；未绑定返回 None。"""
    open_id, union_id = operator_ids(payload)
    binding = FeishuBinding.resolve(union_id=union_id, open_id=open_id)
    return binding.user if binding else None


# ── 响应构造 ──────────────────────────────────────────────

def card_response(card, *, toast=None, toast_type='info'):
    """卡片回调的响应体：直接回一张新卡片（不用再发一次出站请求）。"""
    body = {'card': {'type': 'raw', 'data': card}}
    if toast:
        body['toast'] = {'type': toast_type, 'content': toast[:100]}
    return body


def _reject(title, reason, *, tip=None):
    return card_response(cards.notice(title, reason, tip=tip),
                         toast=reason, toast_type='error')


# ── 审计 ──────────────────────────────────────────────────

def audit(user, summary):
    """手写一条审计。

    回调路径整段在 audit 中间件的 SKIP_PATHS 里（群里每条消息都是一次回调，
    落表只有噪声），但「谁从飞书发起了执行」必须留痕——这里补上，且只在识别出
    操作人之后记，记的是动作本身而不是那坨密文。
    """
    from apps.audit.models import AuditLog

    try:
        AuditLog.objects.create(
            user=user, action='POST', path=AUDIT_PATH, method='POST',
            ip=None, status_code=200,
            body=json.dumps(summary, ensure_ascii=False)[:2000],
        )
    except Exception:  # noqa: BLE001 - 审计写失败不该把执行链路带停
        logger.warning('飞书动作审计写入失败：%s', summary, exc_info=True)


# ── 事件：@机器人 ─────────────────────────────────────────

def handle_event(app, payload):
    """处理一条事件订阅回调。返回 None——事件没有响应体，卡片异步发出去。"""
    kind = event_type(payload)
    if kind != EVENT_MESSAGE_RECEIVE:
        logger.info('飞书事件 %s 未订阅处理，忽略', kind or '(未知)')
        return None

    chat_id, _ = context_ids(payload)
    if not chat_id:
        logger.warning('飞书消息事件缺少 chat_id，忽略')
        return None

    user = resolve_user(payload)
    if user is None:
        open_id, _ = operator_ids(payload)
        logger.info('飞书消息来自未绑定账号 %s', open_id)
        tasks.dispatch_send_card(chat_id, cards.notice(
            '尚未绑定',
            '你的飞书账号还没有绑定平台用户，无法发起任务执行。',
            tip='用飞书登录一次运维平台即可自动绑定（按企业邮箱匹配），'
                '匹配不上时请联系管理员。'))
        return None

    text = message_text(payload)
    if text in ('help', '帮助', '?', '？'):
        tasks.dispatch_send_card(chat_id, _help_card())
        return None

    available = list(access.available_tasks(user, chat_id)[:50])
    tasks.dispatch_send_card(chat_id, cards.task_picker(available))
    return None


def _help_card():
    return cards.notice(
        '运维机器人',
        '发任意消息给我（群里 @我）即可列出可执行的任务流。',
        tip='任务需在平台上开启「允许从飞书发起执行」，且当前会话已被授权；'
            '生产环境的执行会多一步二次确认。')


# ── 卡片动作 ──────────────────────────────────────────────

def handle_card_action(app, payload):
    """处理一次卡片按钮/下拉回调，返回响应体（直接换掉这张卡片）。"""
    value, option = action_payload(payload)
    act = str(value.get('act') or '')
    if act == cards.ACT_CANCEL:
        return card_response(cards.handled('已取消'))

    user = resolve_user(payload)
    if user is None:
        return _reject('尚未绑定',
                       '你的飞书账号还没有绑定平台用户，无法发起任务执行。',
                       tip='用飞书登录一次运维平台即可自动绑定。')

    open_id, _ = operator_ids(payload)
    chat_id, message_id = context_ids(payload)

    if act == cards.ACT_PICK_TASK:
        return _on_pick(user, chat_id, option)
    if act == cards.ACT_RUN:
        return _on_run(user, chat_id, open_id, value, message_id)
    if act == cards.ACT_CONFIRM_PROD:
        return _on_confirm_prod(user, chat_id, open_id, value, message_id)
    if act == cards.ACT_PAUSE:
        # T10：暂停处理（continue/abort、retry/skip/abort）
        logger.info('飞书暂停处理动作待接入：%s', value)
        return None

    logger.info('飞书卡片动作未识别：%s', value)
    return None


def _load_task(task_id):
    try:
        return ExecutionTask.objects.select_related(
            'project_group', 'channel', 'environment').filter(pk=int(task_id)).first()
    except (TypeError, ValueError):
        return None


def _on_pick(user, chat_id, option):
    """下拉选中一条任务 → 确认卡。"""
    task = _load_task(option)
    if task is None:
        return _reject('任务不存在', '该任务已被删除，请重新发起。')
    error = access.check(user, task, chat_id)
    if error:
        return _reject('无法执行', error)
    return card_response(cards.task_detail(
        task, server_count=task.matched_servers().count(),
        step_count=task.steps.count()))


def _on_run(user, chat_id, open_id, value, message_id=''):
    """点「执行」：非生产直接发起，生产再要一次确认。"""
    task = _load_task(value.get('task'))
    if task is None:
        return _reject('任务不存在', '该任务已被删除，请重新发起。')
    # 重新判一次权限：卡片可能是十分钟前发的，期间角色/开关都可能变
    error = access.check(user, task, chat_id)
    if error:
        return _reject('无法执行', error)

    if access.needs_production_confirm(task):
        ttl = getattr(settings, 'FEISHU_CONFIRM_TTL', CONFIRM_TTL)
        token = FeishuConfirmToken.issue(task, user, open_id, chat_id, ttl)
        return card_response(cards.production_confirm(
            task, server_count=task.matched_servers().count(),
            step_count=task.steps.count(), token=token.token, ttl_seconds=ttl))
    return _launch(task, user, chat_id, message_id, confirmed_prod=False)


def _on_confirm_prod(user, chat_id, open_id, value, message_id=''):
    """生产二次确认：消费一次性票据后发起。"""
    token, error = FeishuConfirmToken.consume(value.get('token'), open_id)
    if error:
        return _reject('确认未通过', error)
    if token.user_id != user.id:
        # 票据签给谁就只能由谁用；open_id 已经比过一次，这里再兜一次绑定被改的情况
        return _reject('确认未通过', '只有发起人本人可以确认这次生产执行。')
    task = _load_task(token.task_id)
    if task is None:
        return _reject('任务不存在', '该任务已被删除，请重新发起。')
    # 票据只证明「确认有效」，权限仍按当下重判
    error = access.check(user, task, chat_id)
    if error:
        return _reject('无法执行', error)
    return _launch(task, user, chat_id, message_id, confirmed_prod=True)


def _launch(task, user, chat_id, message_id='', *, confirmed_prod):
    """走 task_exec 的统一发起入口，返回进度卡。"""
    error = trigger.preflight(task)
    if error:
        return _reject('无法执行', error)

    steps = list(task.steps.all())
    selected = list(task.matched_servers())
    log, error = trigger.launch(
        task, steps, selected, picked={},
        triggered_by=user, trigger_type='feishu')
    if error:
        return _reject('无法执行', error)

    # 记住这次执行对应哪条消息：后续的进度与终态就地改这张卡（见 notify.py）。
    # 按钮动作替换的正是这条消息，所以群里始终是同一条消息在原地变化
    notify.bind(log, chat_id, message_id)

    logger.warning('飞书触发任务流 %s（用户 %s，%s 个模块，%s 台主机，log=%s）',
                   task.name, user.username, len(steps), len(selected), log.id)
    audit(user, {
        'act': 'execute', 'task_id': task.id, 'task': task.name,
        'log_id': log.id, 'chat_id': chat_id,
        'step_count': len(steps), 'server_count': len(selected),
        'production_confirmed': confirmed_prod,
    })
    return card_response(cards.progress(task, log),
                         toast=f'已发起执行（{len(steps)} 个模块）', toast_type='success')
