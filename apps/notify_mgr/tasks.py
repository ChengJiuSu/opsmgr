"""通知投递的异步调度。

调度后端沿用 `TASK_EXEC_BACKEND`（thread 默认 / celery 生产 / sync 测试），与
task_exec、im_mgr 同一套口径。celery 走默认队列——**不能进 task_exec 队列**：
那边 worker 并发只有 2，通知挤进去会和任务执行抢执行位。

失败只记日志：通知发不出去绝不能影响业务本身。
"""
import logging
import threading

from django.conf import settings

logger = logging.getLogger(__name__)


def deliver(event_id):
    """投递一个事件；失败只记日志。"""
    from . import dispatch

    try:
        dispatch.deliver(event_id)
    except Exception:  # noqa: BLE001 - 通知链路不该外溢
        logger.warning('通知投递失败：event=%s', event_id, exc_info=True)


def dispatch_deliver(event_id):
    """按配置后端异步投递。"""
    backend = getattr(settings, 'TASK_EXEC_BACKEND', 'thread')
    if backend == 'sync':
        deliver(event_id)
    elif backend == 'celery' and deliver_task is not None:
        deliver_task.delay(event_id)
    else:
        _in_thread(deliver, event_id)


def clean_records(days=None):
    """清理过期的通知事件（发送记录随之级联删除），返回删除的事件数。"""
    from datetime import timedelta

    from django.utils import timezone

    from .models import NotifyEvent

    days = days if days is not None else settings.NOTIFY_RECORD_RETENTION_DAYS
    cutoff = timezone.now() - timedelta(days=days)
    deleted, detail = NotifyEvent.objects.filter(created_at__lt=cutoff).delete()
    return detail.get(NotifyEvent._meta.label, 0)


def _in_thread(func, *args):
    """线程后端：守护线程发出去，调用方不等。进程重启会丢——通知可以接受。"""
    def run():
        from django.db import connection

        try:
            func(*args)
        finally:
            connection.close()

    threading.Thread(target=run, daemon=True).start()


try:  # pragma: no cover - 未装 celery 的环境（开发机）照常工作
    from celery import shared_task

    @shared_task(name='notify_mgr.deliver', ignore_result=True)
    def deliver_task(event_id):
        deliver(event_id)

    @shared_task(name='notify_mgr.clean_records', ignore_result=True)
    def clean_records_task():
        clean_records()
except ImportError:  # pragma: no cover
    deliver_task = clean_records_task = None
