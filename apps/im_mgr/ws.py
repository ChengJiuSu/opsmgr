"""长连接接收事件：把 lark-oapi 的事件对象翻成现有 handlers 认识的 payload。

**为什么有这一层**：回调模式要求平台把两条地址挂在公网上；长连接反过来——平台
主动外连飞书，入站零暴露，服务可以收回内网、只对指定 IP 开放。两种模式的区别
**只在「事件怎么进来」**，进来之后的一切（鉴权、准入、卡片、执行、播报）完全共用，
所以这里只做一件事：把 SDK 的对象转回 `handlers` 一直在用的 payload 形状。

不把 handlers 改成吃 SDK 对象，是为了让两种模式共用同一套代码路径与同一批测试——
否则「回调能跑、长连接不能跑」这种问题会重复出现两遍。

三个必须注意的地方：

- **handler 跑在 asyncio 事件循环里**（SDK 的 `_handle_data_frame` 里同步调用），
  Django ORM 在异步上下文中会抛 `SynchronousOnlyOperation`，所以命令入口设了
  `DJANGO_ALLOW_ASYNC_UNSAFE`。由此推出一条硬约束：**执行后端必须是 celery**，
  `sync` 后端会把 ansible 跑在事件循环里，把整条长连接卡死。
- **数据库连接要回收**：这是个长期运行的进程，连接放久了会被数据库端断掉，
  而 Django 不会自己发现。每次处理前后 `close_old_connections()`。
- **异常一律吞掉**：handler 抛出去会让 SDK 记一条错误日志，但更糟的是一次业务
  异常可能连带影响后续事件的处理。与回调入口同一口径——业务异常不改变对外行为。
"""
import logging

from django.db import close_old_connections

from . import handlers

logger = logging.getLogger(__name__)


def event_payload(event):
    """`CustomizedEvent` → 回调模式那套 payload dict。

    SDK 把 `event` 原样保留成 dict（就是飞书推过来的那坨），header 拆成了对象，
    这里拼回去即可——handlers 读的正是 `header.event_type` 与 `event.*`。
    """
    header = getattr(event, 'header', None)
    return {
        'schema': getattr(event, 'schema', None) or '2.0',
        'header': {
            'event_id': getattr(header, 'event_id', '') or '',
            'event_type': getattr(header, 'event_type', '') or '',
            'token': getattr(header, 'token', '') or '',
        },
        'event': getattr(event, 'event', None) or {},
    }


def card_payload(trigger):
    """`P2CardActionTrigger` → 回调模式那套 payload dict。

    卡片这条 SDK 把整棵树都转成了对象（operator / context / action 各一层），
    所以要逐个摊回 dict。漏一个字段的表现是「点了没反应」，而日志里什么都看不出来
    ——handlers 只会说「动作未识别」。
    """
    header = getattr(trigger, 'header', None)
    data = getattr(trigger, 'event', None)
    operator = getattr(data, 'operator', None)
    context = getattr(data, 'context', None)
    action = getattr(data, 'action', None)
    return {
        'schema': getattr(trigger, 'schema', None) or '2.0',
        'header': {
            'event_id': getattr(header, 'event_id', '') or '',
            'event_type': getattr(header, 'event_type', '') or 'card.action.trigger',
            'token': getattr(header, 'token', '') or '',
        },
        'event': {
            'operator': {
                'open_id': getattr(operator, 'open_id', '') or '',
                'union_id': getattr(operator, 'union_id', '') or '',
                'user_id': getattr(operator, 'user_id', '') or '',
            },
            'context': {
                'open_chat_id': getattr(context, 'open_chat_id', '') or '',
                'open_message_id': getattr(context, 'open_message_id', '') or '',
            },
            'action': {
                'tag': getattr(action, 'tag', '') or '',
                'value': getattr(action, 'value', None) or {},
                'option': getattr(action, 'option', '') or '',
                'name': getattr(action, 'name', '') or '',
                'form_value': getattr(action, 'form_value', None),
                'input_value': getattr(action, 'input_value', '') or '',
            },
        },
    }


def on_event(event):
    """事件订阅（消息、机器人菜单）。"""
    payload = event_payload(event)
    close_old_connections()
    try:
        _dispatch_event(payload)
    except Exception:  # noqa: BLE001 - 一次业务异常不该影响后续事件
        logger.exception('飞书长连接事件处理异常：%s',
                         payload.get('header', {}).get('event_type'))
    finally:
        close_old_connections()


def _dispatch_event(payload):
    from . import dedupe
    from .models import FeishuApp

    event_id = handlers.event_id(payload)
    if event_id:
        # 与回调模式同一道幂等：飞书的投递重试在长连接上同样存在
        try:
            if not dedupe.consume('event', event_id, 3600):
                logger.info('飞书事件重复投递，已忽略：event_id=%s', event_id)
                return
        except dedupe.DedupeUnavailable:
            logger.warning('判重缓存不可用，丢弃事件 event_id=%s（fail-closed）', event_id)
            return
    app = FeishuApp.load()
    if not app.is_ready():
        logger.warning('飞书接入未就绪，忽略事件')
        return
    logger.info('飞书长连接事件：%s', handlers.event_type(payload))
    handlers.handle_event(app, payload)


def on_card_action(trigger):
    """卡片按钮回调。返回值由 SDK 发回飞书（与回调模式的响应体同构）。"""
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        P2CardActionTriggerResponse,
    )

    from .models import FeishuApp

    payload = card_payload(trigger)
    close_old_connections()
    try:
        app = FeishuApp.load()
        if not app.is_ready():
            logger.warning('飞书接入未就绪，忽略卡片动作')
            return None
        logger.info('飞书长连接卡片动作：%s',
                    (payload['event']['action'].get('value') or {}).get('act'))
        body = handlers.handle_card_action(app, payload)
    except Exception:  # noqa: BLE001 - 与回调入口同口径：业务异常不改变对外行为
        logger.exception('飞书长连接卡片动作处理异常')
        return None
    finally:
        close_old_connections()
    # handlers 返回的就是回调模式的响应体，SDK 的响应模型能直接吃这个 dict
    return P2CardActionTriggerResponse(body) if body else None


def build_client(app):
    """按当前配置建一个长连接客户端（不启动）。"""
    import lark_oapi as lark

    handler = (
        lark.EventDispatcherHandler.builder('', '')   # 长连接不走验签，密钥用不上
        .register_p2_customized_event(handlers.EVENT_MESSAGE_RECEIVE, on_event)
        .register_p2_customized_event(handlers.EVENT_BOT_MENU, on_event)
        .register_p2_card_action_trigger(on_card_action)
        .build()
    )
    return lark.ws.Client(app.app_id, app.app_secret, event_handler=handler,
                          log_level=lark.LogLevel.INFO)
