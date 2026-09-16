"""飞书出站的异步调度。

**回调线程里绝不发飞书请求**：卡片回调要求 3 秒内回包，一次出站 HTTP 少则几百
毫秒、多则撞上飞书抖动直接把回调拖超时，飞书随即重试，于是同一件事被处理两次。
所以所有「主动发一张卡」都从这里异步出去。

按钮动作那条路不用走这里：卡片回调可以在响应体里直接回一张新卡片，
一次往返搞定，既快又不会丢（见 handlers.card_response）。

调度后端沿用 `TASK_EXEC_BACKEND`（thread 默认 / celery 生产 / sync 测试），
与 task_exec、cloud_mgr 同一套口径，不再引入第二种约定。

**失败只记日志**：飞书发不出去是飞书的事，绝不能影响任务执行本身，更不能把
异常抛回执行链路。
"""
import logging
import threading

from django.conf import settings

logger = logging.getLogger(__name__)


def send_card(chat_id, card, receive_id_type='chat_id'):
    """同步发一张卡片；失败只记日志，返回 message_id 或 ''。"""
    from . import client

    try:
        return client.send_card(chat_id, card, receive_id_type=receive_id_type)
    except Exception:  # noqa: BLE001 - 飞书侧的任何问题都不该外溢
        logger.warning('飞书发卡失败：receive_id=%s', chat_id, exc_info=True)
        return ''


def update_card(message_id, card):
    """同步更新一张卡片；失败只记日志。"""
    from . import client

    try:
        client.update_card(message_id, card)
        return True
    except Exception:  # noqa: BLE001
        logger.warning('飞书更新卡片失败：message_id=%s', message_id, exc_info=True)
        return False


def dispatch_send_card(chat_id, card, receive_id_type='chat_id'):
    """按配置后端异步发卡。"""
    backend = getattr(settings, 'TASK_EXEC_BACKEND', 'thread')
    if backend == 'sync':
        send_card(chat_id, card, receive_id_type)
    elif backend == 'celery':
        send_card_task.delay(chat_id, card, receive_id_type)
    else:
        _in_thread(send_card, chat_id, card, receive_id_type)


def dispatch_update_card(message_id, card):
    """按配置后端异步更新卡片。"""
    backend = getattr(settings, 'TASK_EXEC_BACKEND', 'thread')
    if backend == 'sync':
        update_card(message_id, card)
    elif backend == 'celery':
        update_card_task.delay(message_id, card)
    else:
        _in_thread(update_card, message_id, card)


def refresh_card(log_id):
    """按 log_id 现读现渲染并更新卡片；失败只记日志。"""
    from . import notify

    try:
        notify.refresh(log_id)
    except Exception:  # noqa: BLE001 - 通知链路不该外溢
        logger.warning('飞书卡片刷新失败：log=%s', log_id, exc_info=True)


def dispatch_refresh_card(log_id):
    """按配置后端异步刷新执行卡片。"""
    backend = getattr(settings, 'TASK_EXEC_BACKEND', 'thread')
    if backend == 'sync':
        refresh_card(log_id)
    elif backend == 'celery':
        refresh_card_task.delay(log_id)
    else:
        _in_thread(refresh_card, log_id)


def pause_push(log_id):
    """私聊推送暂停处理卡；失败只记日志。"""
    from . import notify

    try:
        notify.push_pause(log_id)
    except Exception:  # noqa: BLE001 - 通知链路不该外溢
        logger.warning('飞书暂停卡推送失败：log=%s', log_id, exc_info=True)


def dispatch_pause_push(log_id):
    """按配置后端异步推送暂停处理卡。"""
    backend = getattr(settings, 'TASK_EXEC_BACKEND', 'thread')
    if backend == 'sync':
        pause_push(log_id)
    elif backend == 'celery':
        pause_push_task.delay(log_id)
    else:
        _in_thread(pause_push, log_id)


def _in_thread(func, *args):
    """线程后端：守护线程发出去，请求线程不等。

    进程重启会杀掉守护线程，于是这张卡就丢了——可以接受：卡片是通知，不是状态
    的权威来源，页面上的执行记录始终是准的。
    """
    def run():
        from django.db import connection

        try:
            func(*args)
        finally:
            connection.close()

    threading.Thread(target=run, daemon=True).start()


try:  # pragma: no cover - 未装 celery 的环境（开发机）照常工作
    from celery import shared_task

    @shared_task(ignore_result=True)
    def send_card_task(chat_id, card, receive_id_type='chat_id'):
        send_card(chat_id, card, receive_id_type)

    @shared_task(ignore_result=True)
    def update_card_task(message_id, card):
        update_card(message_id, card)

    @shared_task(ignore_result=True)
    def refresh_card_task(log_id):
        refresh_card(log_id)

    @shared_task(ignore_result=True)
    def pause_push_task(log_id):
        pause_push(log_id)
except ImportError:  # pragma: no cover
    send_card_task = update_card_task = refresh_card_task = pause_push_task = None
