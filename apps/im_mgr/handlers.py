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
#: 机器人自定义菜单被点击。**没有 chat_id**（菜单不属于某个会话），只能按
#: 操作人的 open_id 私聊回卡片；操作人也藏得更深（operator.operator_id）
EVENT_BOT_MENU = 'application.bot.menu_v6'
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
    """点这次按钮 / 发这条消息 / 点菜单的人，返回 (open_id, union_id)。

    三种事件把人放在三个位置，且**菜单事件还多嵌了一层**
    （`operator.operator_id`）——照着卡片回调的路径取会拿到空值，表现是
    「你的飞书账号还没有绑定平台用户」，指向完全错误的方向。
    """
    event = _event(payload)
    operator = event.get('operator')
    holders = [
        operator,
        (operator or {}).get('operator_id') if isinstance(operator, dict) else None,
        (event.get('sender') or {}).get('sender_id'),
    ]
    for holder in holders:
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
    """按钮/下拉的回传体，返回 (value dict, 下拉选中值)。

    卡片 2.0 的回调把值放在 `behaviors[].value`（`value` 字段在某些组件上不再出现），
    表单提交时另有 `form_value`。这里只负责取出「这次点的是什么」，表单里勾了什么
    由调用方读 `form_value`。
    """
    action = _event(payload).get('action') or payload.get('action') or {}
    if not isinstance(action, dict):
        return {}, ''
    value = action.get('value')
    if not value:
        for behavior in (action.get('behaviors') or []):
            if isinstance(behavior, dict) and behavior.get('value'):
                value = behavior['value']
                break
    if isinstance(value, str):
        # 部分客户端会把 value 序列化成字符串回传
        try:
            value = json.loads(value)
        except ValueError:
            value = {}
    return (value if isinstance(value, dict) else {}), str(action.get('option') or '')


def is_p2p(payload):
    """这次交互是不是发生在与机器人的单聊里。

    菜单事件没有会话概念，按单聊处理——它只能是本人点的。
    """
    if event_type(payload) == EVENT_BOT_MENU:
        return True
    message = _event(payload).get('message') or {}
    return str(message.get('chat_type') or '') == 'p2p'


def form_value(payload):
    """卡片 2.0 表单提交时回传的表单值。

    正常是 `{组件name: 值}`，但**不强制转成 dict**：实测飞书在有些形态下直接给
    列表，硬判 dict 会把它吞成空——而「明明勾了却按全量执行」在生产上是不可
    接受的。形状判断留给调用方（见 selected_host_ids）。
    """
    action = _event(payload).get('action') or payload.get('action') or {}
    return action.get('form_value') if isinstance(action, dict) else None


def selected_host_ids(payload):
    """从表单回传里取勾选的主机 id 列表。

    **刻意写得容错**：飞书在不同版本/组件上回传的形状不一致（可能是
    `['1','2']`、`'1,2'`、`[{'value':'1'}]`，也可能整个 form_value 就是一个列表），
    文档讲得不清楚。这里把几种都吃下来——判错的后果是「明明勾了却按全量执行」，
    在生产上是不可接受的，而容错的成本只有这十几行。
    """
    raw = form_value(payload)
    if isinstance(raw, dict):
        raw = raw.get(cards.HOSTS_FIELD, raw.get('hosts'))
    return _flatten_ids(raw)


def _flatten_ids(raw):
    if raw is None:
        return []
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(',') if part.strip()]
    if isinstance(raw, dict):
        # {'value': '1'} 或 {'1': True} 两种都见过
        if 'value' in raw:
            return _flatten_ids(raw['value'])
        return [str(k) for k, v in raw.items() if v]
    if isinstance(raw, (list, tuple)):
        found = []
        for item in raw:
            found.extend(_flatten_ids(item))
        return found
    return [str(raw)]


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
    """处理一条事件订阅回调。返回 None——事件没有响应体，卡片异步发出去。

    两种入口殊途同归，都回一张任务选择卡：

    - `im.message.receive_v1`：群里 @机器人 或私聊发消息，回到该会话；
    - `application.bot.menu_v6`：点机器人自定义菜单。菜单不属于任何会话，
      **payload 里没有 chat_id**，只能按操作人的 open_id 私聊回。
    """
    kind = event_type(payload)
    if kind not in (EVENT_MESSAGE_RECEIVE, EVENT_BOT_MENU):
        logger.info('飞书事件 %s 未订阅处理，忽略', kind or '(未知)')
        return None

    open_id, _ = operator_ids(payload)
    p2p = is_p2p(payload)
    chat_id, _message_id = context_ids(payload)
    # 菜单事件没有 chat_id，用 open_id 当收件人（receive_id_type 随之切换）
    receive_id = chat_id or open_id
    receive_id_type = 'chat_id' if chat_id else 'open_id'
    if not receive_id:
        logger.warning('飞书事件 %s 既没有 chat_id 也没有 open_id，忽略', kind)
        return None

    def reply(card):
        tasks.dispatch_send_card(receive_id, card, receive_id_type=receive_id_type)

    user = resolve_user(payload)
    if user is None:
        logger.info('飞书事件来自未绑定账号 %s', open_id)
        reply(cards.notice(
            '尚未绑定',
            '你的飞书账号还没有绑定平台用户，无法发起任务执行。',
            tip='用飞书登录一次运维平台即可自动绑定（按企业邮箱匹配），'
                '匹配不上时请联系管理员。'))
        return None

    if kind == EVENT_MESSAGE_RECEIVE and message_text(payload) in ('help', '帮助', '?', '？'):
        reply(_help_card())
        return None

    if not access.session_allowed(chat_id, p2p):
        # 列表为空的选择卡只会让人以为「没有任务」，这里把原因说清楚
        reply(cards.notice(
            '当前会话未开放执行',
            '单聊尚未开放发起执行，请到已授权的群里操作。' if p2p
            else '当前群未被授权发起任务执行。',
            tip='管理员可在「系统管理 → 飞书接入」开启。'))
        return None

    available = list(access.available_tasks(user, chat_id, is_p2p=p2p)[:50])
    reply(cards.task_picker(available, p2p=p2p))
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
    # 卡片回调的 context 里**没有 chat_type**，而单聊的 chat_id 同样是 oc_ 开头
    #（飞书没有 p2p_ 这种前缀），从 id 上分不出群聊还是单聊。所以发卡时就把场景
    # 写进按钮 value 带回来；拿不到标记时退回「没有 chat_id 即单聊」
    p2p = bool(value.get('p2p')) or not chat_id

    if act == cards.ACT_PICK_TASK:
        return _on_pick(user, chat_id, option, p2p)
    if act == cards.ACT_PICK_HOSTS:
        return _on_pick_hosts(user, chat_id, value, p2p)
    if act == cards.ACT_RUN:
        return _on_run(user, chat_id, open_id, value, message_id, p2p)
    if act == cards.ACT_RUN_SELECTED:
        return _on_run(user, chat_id, open_id, value, message_id, p2p,
                       selected_ids=selected_host_ids(payload))
    if act == cards.ACT_CONFIRM_PROD:
        return _on_confirm_prod(user, chat_id, open_id, value, message_id, p2p)
    if act == cards.ACT_PAUSE:
        return _on_pause(user, value)
    if act == cards.ACT_REVIEW:
        return _on_review(user, chat_id, value, message_id, p2p)
    if act == cards.ACT_SUDO:
        return _on_sudo(user, open_id, value)
    if act == cards.ACT_PROBE:
        # 组件探针：把飞书回传的 action 原样打回去，用来确认多选组件的数据结构
        action = _event(payload).get('action') or payload.get('action') or {}
        logger.warning('飞书组件探针回传：%s', json.dumps(action, ensure_ascii=False))
        return card_response(cards.probe_result(action))

    logger.info('飞书卡片动作未识别：%s', value)
    return None


def _on_sudo(user, open_id, value):
    """在飞书上确认一次敏感操作。

    这里**只把请求标记为已确认**，真正开 sudo 窗口由网页那边轮询时完成——
    窗口要绑发起它的那个访问令牌 jti，而卡片回调里没有那条会话。
    """
    from .models import FeishuSudoRequest

    row, error = FeishuSudoRequest.approve(value.get('token'), open_id)
    if error:
        return _reject('确认未通过', error)
    if row.user_id != user.id:
        # open_id 已经比过一次，这里兜住「绑定关系在这期间被改了」
        return _reject('确认未通过', '只有发起人本人可以确认这次操作。')
    logger.warning('用户 %s 经飞书确认了敏感操作：%s（来源 IP %s）',
                   row.user.username, row.action or '-', row.client_ip or '-')
    audit(user, {'act': 'sudo_confirm', 'action': row.action,
                 'client_ip': row.client_ip})
    return card_response(cards.handled('已确认，请回到页面继续操作',
                                       by=user.username),
                         toast='已确认', toast_type='success')


def _load_task(task_id):
    try:
        return ExecutionTask.objects.select_related(
            'project_group', 'channel', 'environment').filter(pk=int(task_id)).first()
    except (TypeError, ValueError):
        return None


def _on_pick(user, chat_id, option, p2p=False):
    """下拉选中一条任务 → 确认卡。"""
    task = _load_task(option)
    if task is None:
        return _reject('任务不存在', '该任务已被删除，请重新发起。')
    error = access.check(user, task, chat_id, is_p2p=p2p)
    if error:
        return _reject('无法执行', error)
    return card_response(cards.task_detail(
        task, server_count=len(_target_servers(task)),
        step_count=task.steps.count(), p2p=p2p))


def _target_servers(task):
    """这次执行实际会动到的主机（各模块按服务类型收窄后的并集）。

    全局范围里那些没有任何模块用得上的主机不该出现在卡片上——数字也好、
    多选项也好，都按这个集合来，两处才对得上。
    """
    servers = access.selectable_servers(task)
    return servers if servers is not None else list(task.matched_servers())


def _on_pick_hosts(user, chat_id, value, p2p=False):
    """点「选择主机」：换成多选表单卡，默认全部勾上。"""
    task = _load_task(value.get('task'))
    if task is None:
        return _reject('任务不存在', '该任务已被删除，请重新发起。')
    error = access.check(user, task, chat_id, is_p2p=p2p)
    if error:
        return _reject('无法执行', error)
    servers = _target_servers(task)
    if not servers:
        return _reject('无法执行', '当前范围未匹配到可执行主机。')
    return card_response(cards.host_select(task, servers, p2p=p2p))


def _resolve_selected(task, selected_ids):
    """把回传的主机 id 收敛成本次要执行的主机，返回 (主机列表, 中文错误)。

    **按当前匹配集重新解析**，不信卡片给的值：卡片可能是几分钟前发的，期间主机
    可能已下线、改了维度或取消了服务规划。这与页面 execute 接口的口径一致
    （那边是「主机 id 不在全局范围匹配集内，请刷新执行页面后重试」）。
    """
    matched = {str(s.id): s for s in _target_servers(task)}
    if not selected_ids:
        return list(matched.values()), None
    picked, stale = [], []
    for raw in selected_ids:
        server = matched.get(str(raw))
        (picked if server else stale).append(server or raw)
    if stale:
        return None, (f'有 {len(stale)} 台主机已不在当前范围内（可能已下线或改了维度），'
                      f'请重新发起选择。')
    if not picked:
        return None, '没有选中任何主机。'
    return picked, None


def _on_run(user, chat_id, open_id, value, message_id='', p2p=False, selected_ids=None):
    """点「执行」：非生产直接发起，生产再要一次确认。"""
    task = _load_task(value.get('task'))
    if task is None:
        return _reject('任务不存在', '该任务已被删除，请重新发起。')
    # 重新判一次权限：卡片可能是十分钟前发的，期间角色/开关都可能变
    error = access.check(user, task, chat_id, is_p2p=p2p)
    if error:
        return _reject('无法执行', error)

    servers, error = _resolve_selected(task, selected_ids)
    if error:
        return _reject('无法执行', error)

    if access.needs_production_confirm(task):
        ttl = getattr(settings, 'FEISHU_CONFIRM_TTL', CONFIRM_TTL)
        # 选中的主机随票据存服务端：卡片上的值只用于展示，而这批 id 决定生产上
        # 到底动哪几台
        token = FeishuConfirmToken.issue(task, user, open_id, chat_id, ttl,
                                         server_ids=[s.id for s in servers])
        return card_response(cards.production_confirm(
            task, server_count=len(servers),
            step_count=task.steps.count(), token=token.token, ttl_seconds=ttl,
            p2p=p2p))
    return _launch(task, user, chat_id, message_id, confirmed_prod=False, servers=servers)


def _on_confirm_prod(user, chat_id, open_id, value, message_id='', p2p=False):
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
    error = access.check(user, task, chat_id, is_p2p=p2p)
    if error:
        return _reject('无法执行', error)
    # 票据里存的是签发那一刻选中的主机，这里仍要按当前匹配集重解析一次
    servers, error = _resolve_selected(task, token.server_ids)
    if error:
        return _reject('无法执行', error)

    if access.needs_dual_review(task):
        if p2p:
            # 单聊里那张卡只有发起人看得见，没有第二个人能点——与其发出去让人
            # 卡在那里猜，不如当场说清楚
            return _reject('需要复核',
                           '该执行需要另一个人复核，而单聊里只有你能看到卡片。'
                           '请到已授权的运维群里发起。')
        ttl = getattr(settings, 'FEISHU_REVIEW_TTL', REVIEW_TTL)
        review = FeishuConfirmToken.issue(
            task, user, open_id, chat_id, ttl,
            server_ids=[s.id for s in servers], stage='review')
        logger.warning('飞书生产执行进入复核：%s（发起人 %s，%s 台主机）',
                       task.name, user.username, len(servers))
        return card_response(
            cards.review_request(task, user.username, len(servers),
                                 task.steps.count(), review.token, ttl),
            toast='已提交复核', toast_type='info')

    return _launch(task, user, chat_id, message_id, confirmed_prod=True, servers=servers)


#: 等待复核的有效期（秒）。比发起人确认那道长得多——那是「当场确认」，
#: 这是要等另一个人看到消息、判断、再点
REVIEW_TTL = 600


def _on_review(reviewer, chat_id, value, message_id='', p2p=False):
    """另一个人点「复核通过」。

    复核人要过的门与发起人**完全一样**（能力位、生产权限、数据范围），另加一条
    「不能是发起人自己」——否则复核只是找个人点一下，不构成第二道判断。
    """
    token, error = FeishuConfirmToken.consume(value.get('token'), open_id=None)
    if error:
        return _reject('复核未通过', error)

    task = _load_task(token.task_id)
    if task is None:
        return _reject('任务不存在', '该任务已被删除，请重新发起。')
    error = access.review_error(reviewer, task, token.user_id, chat_id)
    if error:
        # 票据已经被消费掉了：复核人不够格时不该让这张卡还能被别人再点一次——
        # 重新发起比留一张「谁都可以试试」的卡安全
        return _reject('复核未通过', f'{error}请重新发起执行。')

    servers, error = _resolve_selected(task, token.server_ids)
    if error:
        return _reject('无法执行', error)

    FeishuConfirmToken.objects.filter(pk=token.pk).update(reviewer=reviewer)
    logger.warning('飞书生产执行通过复核：%s（发起人 %s，复核人 %s）',
                   task.name, token.user.username, reviewer.username)
    # 复核单独记一条：执行那条记在发起人名下，「谁复核了什么」要能独立查出来
    audit(reviewer, {'act': 'review', 'task_id': task.id, 'task': task.name,
                     'initiator': token.user.username,
                     'reviewer': reviewer.username,
                     'server_count': len(servers)})
    return _launch(task, token.user, chat_id, message_id,
                   confirmed_prod=True, servers=servers, reviewer=reviewer)


#: 暂停处理动作 → 定格卡片上的说法
PAUSE_LABELS = {
    'continue': '已继续执行',
    'abort': '已中止任务',
    'retry': '已重试该模块',
    'skip': '已跳过该模块，继续后续',
}


def _on_pause(user, value):
    """处理「等待确认」/「失败待处理」。

    **不判会话白名单**：这张卡可能是页面发起后私聊推过来的，那个会话不可能在
    白名单里。白名单管的是「哪里能摆出执行入口」，而这里处理的是一次已经发起的
    执行——判的是人的权限（见 access.check_pause）。
    """
    from apps.task_exec.models import TaskExecutionLog

    log = (TaskExecutionLog.objects.select_related(
        'task', 'task__project_group', 'task__channel', 'task__environment')
        .filter(pk=value.get('log')).first())
    if log is None:
        return _reject('无法处理', '该执行记录已被删除。')
    task = log.task

    error = access.check_pause(user, task)
    if error:
        return _reject('无法处理', error)

    # 卡片可能是上一次执行留下的：任务本体的 paused 状态只属于最近一次执行，
    # 拿旧卡片去点等于对着另一次执行下指令
    latest_id = (task.logs.order_by('-created_at', '-id')
                 .values_list('id', flat=True).first())
    if latest_id != log.id:
        return _reject('无法处理', '这张卡片对应的执行已经结束，请查看最新的执行记录。')

    do = str(value.get('do') or '')
    error = trigger.apply_pause_action(task.id, do)
    if error:
        return _reject('无法处理', error)

    label = PAUSE_LABELS.get(do, '已处理')
    logger.warning('飞书处理任务流 %s 的暂停：%s（用户 %s，log=%s）',
                   task.name, do, user.username, log.id)
    audit(user, {'act': 'pause', 'do': do, 'task_id': task.id, 'task': task.name,
                 'log_id': log.id})
    return card_response(cards.handled(label, by=user.username),
                         toast=label, toast_type='success')


def _launch(task, user, chat_id, message_id='', *, confirmed_prod, servers=None,
            reviewer=None):
    """走 task_exec 的统一发起入口，返回进度卡。"""
    error = trigger.preflight(task)
    if error:
        return _reject('无法执行', error)

    steps = list(task.steps.all())
    selected = list(servers) if servers is not None else _target_servers(task)
    log, error = trigger.launch(
        task, steps, selected, picked={},
        triggered_by=user, trigger_type='feishu')
    if error:
        return _reject('无法执行', error)

    # 记住这次执行对应哪条消息：后续的进度与终态就地改这张卡（见 notify.py）。
    # 按钮动作替换的正是这条消息，所以群里始终是同一条消息在原地变化
    notify.bind(log, chat_id, message_id)

    logger.warning('飞书触发任务流 %s（用户 %s，%s 个模块，%s 台主机，log=%s%s）',
                   task.name, user.username, len(steps), len(selected), log.id,
                   f'，复核人 {reviewer.username}' if reviewer else '')
    audit(user, {
        'act': 'execute', 'task_id': task.id, 'task': task.name,
        'log_id': log.id, 'chat_id': chat_id,
        'step_count': len(steps), 'server_count': len(selected),
        'production_confirmed': confirmed_prod,
        'hosts_picked': servers is not None,
        'reviewed_by': getattr(reviewer, 'username', None),
    })
    toast = f'已发起执行（{len(steps)} 个模块）'
    if reviewer:
        toast = f'复核通过，已发起执行（{len(steps)} 个模块）'
    return card_response(cards.progress(task, log), toast=toast, toast_type='success')
