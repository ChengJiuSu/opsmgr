"""排队可见性：pending 任务在执行队列里排第几、前面是谁、为什么还没跑。

pending 有两种来由，页面上要分开说：
  - 在队列里等执行位：celery worker 的子进程都被占着（并发见
    deploy/opsmgr-celery-exec.conf）；
  - 已被取出但撞了主机锁（host_lock.py）：等占用方跑完，tasks._wait_for_lock
    把原因写进任务快照 output，以 LOCK_WAIT_PREFIX 开头。

位次按执行记录 id 排（= 发起顺序 = 投递顺序）。撞锁重投会排到队尾，所以位次只是
近似值；web 进程看不到 broker 里的真实队列，也不该为一个提示去连 broker。
"""
from datetime import timedelta

from django.utils import timezone

LOCK_WAIT_PREFIX = '⏳ 排队等待主机锁'
# 只看这个窗口内的 pending/running 记录：worker 被 kill -9 遗留的僵尸记录会永远停在
# pending/running，不设窗口的话位次会被它们永久抬高。主机锁等待上限是 1 小时，
# 正常排队不会接近一天。
WINDOW = timedelta(days=1)
# 前方/执行中各列几个任务名，再多只给总数
NAME_LIMIT = 5


def _names(qs):
    return list(qs.values_list('task__name', flat=True)[:NAME_LIMIT])


def queued_log(task):
    """任务当前排队中的执行记录（窗口内最新一条 pending）；没有则 None。

    任务本体的 status=pending 不能当「排队中」：从没执行过的任务默认也是 pending。
    真正在排队的标志是有一条还没开跑的执行记录。
    """
    return (task.logs.filter(status='pending', created_at__gte=timezone.now() - WINDOW)
            .order_by('-id').only('id').first())


def queue_info(task):
    """pending 任务的排队信息；非 pending 或找不到排队记录时返回 None。

    {position, ahead_count, ahead, running_count, running, lock_wait}
    lock_wait 为撞锁提示原文（未撞锁为空串）。
    """
    from .models import TaskExecutionLog

    if task.status != 'pending':
        return None
    log = queued_log(task)
    if log is None:
        return None
    recent = TaskExecutionLog.objects.filter(created_at__gte=timezone.now() - WINDOW)
    ahead = recent.filter(status='pending', id__lt=log.id).order_by('id')
    running = recent.filter(status='running').order_by('started_at', 'id')
    ahead_count = ahead.count()
    output = task.output or ''
    return {
        'position': ahead_count + 1,
        'ahead_count': ahead_count,
        'ahead': _names(ahead),
        'running_count': running.count(),
        'running': _names(running),
        'lock_wait': output if output.startswith(LOCK_WAIT_PREFIX) else '',
    }
