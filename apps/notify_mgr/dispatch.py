"""通知投递：事件 → 匹配规则 → 渲染 → 逐目标发送 → 落发送记录。

跑在异步后端里（tasks.dispatch_deliver），不在业务线程里。

- **同一事件对同一目标只发一次**：`NotifyDelivery(event, target_key)` 唯一。两条
  规则指向同一个群时不重复发，异步任务被重投时也不重复发；
- **渲染一次，发多处**：builder 现读库现渲染，一个事件只调一次；
- **单个目标失败不影响其他目标**，失败原因落记录，不重试——通知不是状态的权威
  来源，页面上的执行记录始终是准的。
"""
import logging

from django.db import IntegrityError, transaction
from django.utils import timezone

from . import events
from .models import NotifyDelivery, NotifyEvent, NotifyRule

logger = logging.getLogger(__name__)


class SendError(Exception):
    pass


def render_card(message, mention=False):
    """Message → 飞书卡片 2.0。"""
    from apps.im_mgr import cards

    elements = list(message.elements)
    if mention and message.mention_open_ids:
        elements.append(cards.md(' '.join(
            f'<at id={open_id}></at>' for open_id in message.mention_open_ids)))
    return cards.card(message.title, elements, template=message.template)


def send_to(rule, card):
    """按规则的发送方式把卡片发出去；失败抛 SendError。"""
    if rule.target_type == NotifyRule.TARGET_FEISHU_CHAT:
        from apps.im_mgr import client
        from apps.im_mgr.models import FeishuApp

        if not FeishuApp.load().is_ready():
            raise SendError('飞书应用未启用或配置不全')
        chat = rule.feishu_chat
        try:
            client.send_card(chat.chat_id, card, receive_id_type=chat.receive_id_type)
        except client.FeishuError as exc:
            raise SendError(str(exc)) from exc
        return
    from . import webhook

    try:
        webhook.send_card(rule.webhook_robot, card)
    except webhook.WebhookError as exc:
        raise SendError(str(exc)) from exc


def matching_rules(event):
    rules = (NotifyRule.objects.filter(enabled=True)
             .select_related('feishu_chat', 'webhook_robot')
             .prefetch_related('project_groups', 'channels', 'environments'))
    return [rule for rule in rules if rule.matches(event)]


def _claim(event, rule):
    """为（事件, 目标）占一条发送记录；已有记录（已发过/正在发）返回 None。"""
    try:
        with transaction.atomic():
            return NotifyDelivery.objects.create(
                event=event, rule=rule, target_type=rule.target_type,
                target_key=rule.target_key, rule_name=rule.name,
                target_name=rule.target_name)
    except IntegrityError:
        return None


def deliver(event_id):
    """把一个事件发给所有命中的目标。"""
    event = NotifyEvent.objects.filter(pk=event_id).first()
    if event is None:
        return
    event_type = events.get(event.code)
    if event_type is None:
        return
    rules = [rule for rule in matching_rules(event) if rule.target_key]
    if not rules:
        return
    message = event_type.builder(event)
    if message is None:
        return
    cards = {}
    for rule in rules:
        delivery = _claim(event, rule)
        if delivery is None:
            continue
        delivery.title = message.title[:200]
        if not rule.target_ready():
            _finish(delivery, 'skipped', '发送目标已停用')
            continue
        if rule.target_key in message.exclude_targets:
            _finish(delivery, 'skipped', '该会话已有实时刷新的执行卡片')
            continue
        mention = rule.mention_trigger
        if mention not in cards:
            cards[mention] = render_card(message, mention=mention)
        try:
            send_to(rule, cards[mention])
        except SendError as exc:
            logger.warning('通知发送失败：event=%s rule=%s：%s', event.pk, rule.pk, exc)
            _finish(delivery, 'failed', str(exc))
        except Exception as exc:  # noqa: BLE001 - 单个目标的问题不能影响其余目标
            logger.warning('通知发送异常：event=%s rule=%s', event.pk, rule.pk, exc_info=True)
            _finish(delivery, 'failed', f'发送异常：{exc}')
        else:
            _finish(delivery, 'success')


def _finish(delivery, status, error=''):
    delivery.status = status
    delivery.error = error[:2000]
    delivery.sent_at = timezone.now()
    delivery.save(update_fields=['title', 'status', 'error', 'sent_at'])


def send_test(rule):
    """给规则的目标发一条测试消息；返回错误信息，成功返回 None。"""
    from apps.im_mgr import cards

    card = cards.card('通知测试', [
        cards.md(f'这是一条来自运维平台的测试消息。\n规则：**{cards.esc(rule.name)}**'),
        cards.note(f'发送时间 {timezone.localtime().strftime("%Y-%m-%d %H:%M:%S")}'),
    ], template='blue')
    try:
        send_to(rule, card)
    except SendError as exc:
        return str(exc)
    return None


def send_robot_test(robot):
    """不经规则，直接测试一个群机器人能不能发通。"""
    from apps.im_mgr import cards

    from . import webhook

    card = cards.card('群机器人测试', [
        cards.md(f'机器人 **{cards.esc(robot.name)}** 已接通运维平台。'),
        cards.note(f'发送时间 {timezone.localtime().strftime("%Y-%m-%d %H:%M:%S")}'),
    ], template='blue')
    try:
        webhook.send_card(robot, card)
    except webhook.WebhookError as exc:
        return str(exc)
    return None
