"""主机锁：不同任务同时动同一台主机的同一服务时，后来者排队。

任务执行 worker 开了并发（deploy/ops-manager-celery-exec.conf）之后，两个任务
可能同时落到同一台主机上——部署和重启撞在一起，结果取决于谁先跑到哪一步。
同一任务不会并发（trigger.preflight 拦住了），这里管的是不同任务之间。

锁粒度是「主机 × 服务类型」，键形如 ``<server_id>:<service_type_id>``：
  - 模块选了服务类型：只锁该主机上的这个服务，同机不同服务照常并行；
  - 模块没选服务类型：``<server_id>:*`` 锁整台主机，与该主机上任何键都冲突。

加锁时机是任务开跑前一次性拿齐全部模块的键，跑完才放（终态即释放），而不是
逐模块加放：逐模块的话 A 跑完第一步、B 插进来、A 第二步又等 B，任务流就被拆散
交错执行了，这正是要避免的。代价是锁的范围按整条任务流算，偏保守。

持锁状态不另建表：持锁 = TaskExecutionLog.status == 'running' 且 lock_keys 里
有这个键。执行结束状态落终态，锁自然释放，不存在「忘了解锁」。判冲突与写入
running 必须原子，用缓存互斥（生产 Redis，跨 web/worker 进程）串起来。

已知边界：worker 进程被 kill -9 时持锁记录会永远停在 running（这是既有问题，
任务本身也会卡在执行中），后来者等满 TASK_HOST_LOCK_WAIT_TIMEOUT 记失败，
失败信息里带着持锁任务名，按名去处理那条卡死的记录。
"""
import logging
import time
import uuid
from contextlib import contextmanager

from django.core.cache import cache

logger = logging.getLogger('apps.task_exec')

WILDCARD = '*'
MUTEX_KEY = 'task_exec:host_lock:mutex'
# 互斥只包住「查 running 记录 + 写一行」，毫秒级；TTL 只防持有者崩溃后死锁
MUTEX_TTL = 30
MUTEX_WAIT = 10


def lock_keys(task):
    """任务本次执行要持有的锁键（去重排序）。暂停确认模块不碰主机，不加锁。"""
    from .runner import _step_servers

    keys = set()
    for step in task.steps.all():
        if step.task_type == 'pause':
            continue
        scope = step.service_type_id or WILDCARD
        for server in _step_servers(step, task):
            keys.add(f'{server.id}:{scope}')
    return sorted(keys)


def _split(key):
    server_id, _, scope = key.partition(':')
    return server_id, scope


def conflicts(mine, theirs):
    """两组锁键中相互冲突的、属于 mine 的键。同主机且服务相同或任一方为整机锁即冲突。"""
    held = {}
    for key in theirs or []:
        server_id, scope = _split(key)
        held.setdefault(server_id, set()).add(scope)
    hit = []
    for key in mine:
        server_id, scope = _split(key)
        scopes = held.get(server_id)
        if scopes and (scope == WILDCARD or WILDCARD in scopes or scope in scopes):
            hit.append(key)
    return hit


@contextmanager
def mutex(key, wait=MUTEX_WAIT, ttl=MUTEX_TTL):
    """缓存互斥（生产 Redis，跨 web/worker 进程）。yield 是否拿到；拿不到不抛错，
    由调用方决定是重试还是回错误。只包「查 + 写」这种毫秒级临界区，TTL 只防
    持有者崩溃后死锁。"""
    token = uuid.uuid4().hex
    deadline = time.monotonic() + wait
    got = False
    while True:
        if cache.add(key, token, ttl):
            got = True
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    try:
        yield got
    finally:
        # 只删自己的：持有超过 TTL 后键可能已被别人拿走
        if got and cache.get(key) == token:
            cache.delete(key)


def try_acquire(log, keys):
    """尝试为本次执行加锁；成功则原子地把执行记录置为 running 并写入 lock_keys。

    返回 None 表示已拿到锁；否则返回阻塞原因 {'task_id','task_name','log_id','keys'}
    （拿不到互斥时 task_name 为空，按「稍后重试」处理）。
    """
    from .models import TaskExecutionLog

    with mutex(MUTEX_KEY) as got:
        if not got:
            logger.warning('主机锁互斥等待超时，执行记录 %s 稍后重试', log.pk)
            return {'task_id': None, 'task_name': '', 'log_id': None, 'keys': []}
        if keys:
            holders = (TaskExecutionLog.objects.filter(status='running')
                       .exclude(pk=log.pk)
                       .values('pk', 'task_id', 'task__name', 'lock_keys'))
            for holder in holders:
                hit = conflicts(keys, holder['lock_keys'])
                if hit:
                    return {'task_id': holder['task_id'], 'task_name': holder['task__name'],
                            'log_id': holder['pk'], 'keys': hit}
        TaskExecutionLog.objects.filter(pk=log.pk).update(status='running', lock_keys=keys)
        log.status, log.lock_keys = 'running', keys
        return None


def describe(blocker):
    """撞锁提示（实时面板与超时失败信息共用）。"""
    from apps.asset_mgr.models import Server

    if not blocker['task_name']:
        return '主机锁繁忙，稍后自动重试'
    ids = {_split(k)[0] for k in blocker['keys']}
    names = '、'.join(Server.objects.filter(pk__in=ids).order_by('name').values_list('name', flat=True))
    return f'主机 {names or "-"} 正被任务「{blocker["task_name"]}」（执行记录 #{blocker["log_id"]}）占用'
