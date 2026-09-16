"""飞书回调的业务路由（事件 / 卡片动作）。

与 views.py 的分工：视图只负责「这条请求可不可信」（来源 IP、验签、解密、
幂等）与「怎么回包」，业务落在这里。分开是为了让那道门保持薄——门上的每一行
改动都直接影响公网入口的安全性，不该跟卡片交互逻辑混在一个文件里。

本期（T3）只做路由骨架：认出事件类型、记日志、返回空动作。真正的交互在后续：

- T8：`@bot 执行` → 任务选择卡 → 执行卡 → 生产二次确认（一次性 token）
- T9：执行进度卡片刷新
- T10：暂停确认（continue/abort、retry/skip/abort）

**返回值约定**：两个入口都返回 dict 或 None。None = 无附加响应，视图回恒定包；
dict 会被视图原样作为响应体（卡片回调可借此回 toast 或整张新卡片）。

**输入一律当不可信数据**：payload 来自公网，即便验签通过也只说明「确实是飞书
转发的」，内容仍是群里的人打的字。取值一律带默认、不做 eval/格式化拼接。
"""
import logging

logger = logging.getLogger(__name__)

#: 事件订阅：v2 事件类型在 header.event_type
EVENT_MESSAGE_RECEIVE = 'im.message.receive_v1'


def event_type(payload):
    header = payload.get('header')
    if isinstance(header, dict):
        return str(header.get('event_type') or '')
    # v1 形态：{"type": "event_callback", "event": {"type": "message"}}
    event = payload.get('event')
    if isinstance(event, dict):
        return str(event.get('type') or '')
    return ''


def event_id(payload):
    header = payload.get('header')
    if isinstance(header, dict):
        return str(header.get('event_id') or '')
    return str(payload.get('uuid') or '')


def handle_event(app, payload):
    """处理一条事件订阅回调。"""
    kind = event_type(payload)
    if kind == EVENT_MESSAGE_RECEIVE:
        # T8：解析 @bot 指令，回任务选择卡片
        logger.info('飞书事件 %s 已接收（交互能力待 T8 接入）', kind)
        return None
    logger.info('飞书事件 %s 未订阅处理，忽略', kind or '(未知)')
    return None


def handle_card_action(app, payload):
    """处理一次卡片按钮回调。"""
    action = payload.get('action') or {}
    value = action.get('value') if isinstance(action, dict) else None
    logger.info('飞书卡片动作已接收（交互能力待 T8 接入）：value=%s', value)
    return None
