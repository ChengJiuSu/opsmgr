"""任务执行调度层。

支持三种后端（settings.TASK_EXEC_BACKEND）：
  - 'thread'（默认）：后台线程异步执行，无需 Redis/Celery，适合开发/小规模。
    注意 ansible 子进程与 O(n) 的日志汇聚都跑在 web 进程里，会和 API 抢 GIL 与
    CPU —— 生产用它会让后台响应随任务输出量一起变慢，故生产应选 celery。
  - 'celery'：投递到独立队列 TASK_EXEC_QUEUE，由专用 worker 消费（部署见
    deploy/opsmgr-celery-exec.conf），执行与 web 进程彻底分家。
  - 'sync'：当前请求线程内同步执行，便于测试。

每次执行（含重跑）在 TaskExecutionLog 落一条日志记录：dispatch 时创建（pending），
执行结束回填结果；任务本体字段仅作为「最近一次执行」快照。

执行期间输出由 live.LiveSink 边跑边写：数据库存尾部预览（前端轮询实时刷新），
完整日志流式落盘到 TASK_LOG_DIR/<task_id>/<log_id>.log（按保留期定期清理）。

开跑前先拿主机锁（host_lock.py）：撞上别的任务正在动的「主机 × 服务」时不执行，
保持 pending 并按后端重新排队——celery 带 countdown 重新投递（不占执行位），
thread/sync 在原线程里 sleep 重试。
"""
import logging
import time
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.utils import timezone

from . import host_lock, runner
from .exec_queue import LOCK_WAIT_PREFIX
from .live import (DB_PREVIEW_LIMIT, PREVIEW_LIMIT, SEP, LiveSink, log_rel_path, preview_of, push,
                   push_status, split_note)

logger = logging.getLogger('apps.task_exec')

# 数据库内输出预览上限（字符），完整输出见落盘文件
OUTPUT_PREVIEW_LIMIT = DB_PREVIEW_LIMIT

# 任务执行专用队列。不与证书签发/分发（默认 celery 队列）共用：acme.sh 的 DNS
# 验证上限 900s，一个正在签发的证书会把后面排队的 ansible 任务堵十几分钟，而
# 页面上只是一条「执行中」且无报错的任务。消费者的 -Q 必须与此一致。
TASK_EXEC_QUEUE = 'task_exec'


def execute_task(task_id, log_id=None, wait_since=None):
    """加载任务并执行，同时更新任务快照与执行日志。可在线程/celery/同步上下文中调用。

    撞主机锁时不执行，返回首次撞锁的时间戳（调用方据此稍后重试并原样传回
    wait_since）；其余情况（执行完毕、任务不存在、等锁超时记失败）返回 None。
    """
    from .models import ExecutionTask, TaskExecutionLog

    task = ExecutionTask.objects.filter(pk=task_id).select_related('credential').first()
    if task is None:
        logger.warning('任务 %s 不存在，跳过执行', task_id)
        return

    log = TaskExecutionLog.objects.filter(pk=log_id).first() if log_id else None
    if log is None:
        # 兜底：直接调用（未经 dispatch）时也保证有日志记录
        log = TaskExecutionLog.objects.create(task=task, triggered_by=task.created_by)

    try:
        keys = host_lock.lock_keys(task)
    except Exception:  # noqa: BLE001 - 算不出锁键宁可不加锁，也不能让任务卡在 pending
        logger.exception('任务 %s 计算主机锁失败，本次不加锁', task_id)
        keys = []
    blocker = host_lock.try_acquire(log, keys)
    if blocker:
        return _wait_for_lock(task, log, blocker, wait_since)

    resume = _resume_of(log)
    rel_path = log_rel_path(task.pk, log.pk)
    abs_path = Path(settings.TASK_LOG_DIR) / rel_path
    sink = LiveSink(task.pk, log.pk, log_path=str(abs_path))

    if resume:
        # 失败续跑：沿用本条执行记录（开始时间、输出都接着上次），不清预览
        prior = _read_prior_output(abs_path, log)
        resume['output'] = prior
        resume['results'] = log.steps_result or []
        sink.resume_from(prior)
        task.status = log.status = 'running'
        # 等锁期间任务快照的 output 被换成了排队提示，开跑先换回上次输出的尾部
        task.output, task.output_offset = preview_of(prior), len(prior)
        task.save(update_fields=['status', 'output', 'output_offset'])
        log.save(update_fields=['status'])
        tail = prior[-PREVIEW_LIMIT:]
        push(task.pk, {'kind': 'full', 'output': tail,
                       'start': len(prior) - len(tail), 'offset': len(prior)})
    else:
        started_at = timezone.now()
        task.status = log.status = 'running'
        task.started_at = log.started_at = started_at
        # 上一次执行的输出预览必须清掉：实时面板会把「库内尾部预览」与「本次执行的落盘
        # 文件」按 output_offset 拼接，留着上次的预览会拼出一段前后不属于同一次执行的日志
        task.output, task.output_offset = '', 0
        task.save(update_fields=['status', 'started_at', 'output', 'output_offset'])
        log.save(update_fields=['status', 'started_at'])
        # 开跑：清掉面板上的排队提示（或上次执行残留），再推送 running
        push(task.pk, {'kind': 'full', 'output': '', 'start': 0, 'offset': 0})
    push_status(task.pk)

    rc, output, status, steps_result, checkpoint = -1, '', 'failed', [], None
    try:
        rc, output, steps_result, checkpoint = runner.run(task, sink=sink, resume=resume)
        output = output or ''
        status = 'success' if rc == 0 else 'failed'
        sink.close(final_output=output)
    except Exception as exc:  # noqa: BLE001 - 兜底，保证状态落库
        logger.exception('任务 %s 执行异常', task_id)
        note = f'执行异常：{exc}'
        partial = sink.render()[0]
        sink.close(final_output=partial, note=note)
        rc, output, status = -1, (f'{partial}\n\n{note}' if partial else note), 'failed'
        checkpoint = None
    finally:
        finished_at = timezone.now()
        preview = preview_of(output)
        # 失败断点：执行照常落 failed（释放执行位与主机锁），任务挂上「失败待处理」，
        # 截止前可重试/跳过（续跑）或终止，见 trigger.resolve_failure
        deadline = (finished_at + timedelta(seconds=checkpoint.pop('timeout'))
                    if checkpoint else None)

        task.return_code, task.output, task.status, task.finished_at = rc, preview, status, finished_at
        task.output_offset = len(output)
        task.pause_type = 'failed' if checkpoint else ''
        task.pause_action = ''
        task.resume_deadline = deadline
        task.save(update_fields=[
            'return_code', 'output', 'output_offset', 'status', 'finished_at',
            'pause_type', 'pause_action', 'resume_deadline'])

        log.return_code, log.output, log.status, log.finished_at = rc, preview, status, finished_at
        log.steps_result = steps_result
        log.resume_state = checkpoint
        # 流式落盘成功即回填路径（详情页/下载读全量日志），落盘失败退回预览
        log.log_file = rel_path if abs_path.exists() else ''
        log.save(update_fields=['return_code', 'output', 'status', 'finished_at', 'log_file',
                                'steps_result', 'resume_state'])

        # 收尾推送：整段最终输出 + 终态（客户端据此停连接并刷新执行记录）。
        # 推送按实时窗口取尾部，比写库那份宽——面板上收尾后还要能往回翻几屏，
        # 而库里那份只是轮询回退与文件过期后的兜底。
        final_tail = output[-PREVIEW_LIMIT:] if len(output) > PREVIEW_LIMIT else output
        push(task.pk, {
            'kind': 'full', 'output': final_tail,
            'start': len(output) - len(final_tail), 'offset': len(output),
        })
        push(task.pk, {'kind': 'steps', 'steps': steps_result})
        push_status(task.pk)


def _resume_of(log):
    """本条执行记录是不是一次失败续跑：断点上带了人工选的 action 才算。"""
    state = log.resume_state or {}
    return dict(state) if state.get('action') in ('retry', 'skip') else None


def _read_prior_output(abs_path, log):
    """续跑前读回上次的完整输出（落盘文件），落盘缺失时退回库内预览。"""
    try:
        return abs_path.read_text(encoding='utf-8')
    except OSError:
        return split_note(log.output or '')[1]


def _wait_for_lock(task, log, blocker, wait_since):
    """撞锁：未超时则在面板上给出排队提示并返回 wait_since；超时记失败返回 None。"""
    from .models import ExecutionTask, TaskExecutionLog

    now = time.time()
    wait_since = wait_since or now
    waited = int(now - wait_since)
    timeout = settings.TASK_HOST_LOCK_WAIT_TIMEOUT
    reason = host_lock.describe(blocker)

    if waited >= timeout:
        note = f'等待主机锁超时（已等 {waited}s，上限 {timeout}s），本次未执行：{reason}'
        logger.warning('任务 %s 执行记录 %s %s', task.pk, log.pk, note)
        if _resume_of(log):
            _restore_failure_wait(task, log, note)
            return None
        finished_at = timezone.now()
        ExecutionTask.objects.filter(pk=task.pk).update(
            status='failed', return_code=-1, output=note, output_offset=len(note), finished_at=finished_at)
        TaskExecutionLog.objects.filter(pk=log.pk).update(
            status='failed', return_code=-1, output=note, finished_at=finished_at)
        push(task.pk, {'kind': 'full', 'output': note, 'start': 0, 'offset': len(note)})
        push_status(task.pk)
        return None

    note = (f'{LOCK_WAIT_PREFIX}：{reason}。已等 {waited}s，每 '
            f'{settings.TASK_HOST_LOCK_RETRY}s 重试，超过 {timeout}s 记失败。')
    # 只写任务快照（面板/轮询读它），执行记录的 output 留给真正的执行输出
    ExecutionTask.objects.filter(pk=task.pk).update(output=note, output_offset=len(note))
    push(task.pk, {'kind': 'full', 'output': note, 'start': 0, 'offset': len(note)})
    return wait_since


def _restore_failure_wait(task, log, note):
    """续跑等主机锁超时：退回「失败待处理」，断点保留，可再次处理。

    截止时间从现在重新起算——等锁的这段时间里没人能处理它。
    """
    from .models import ExecutionTask, TaskExecutionLog

    state = dict(log.resume_state or {})
    state.pop('action', None)
    deadline = timezone.now() + timedelta(seconds=max(runner.failure_wait_timeout(), 60))
    TaskExecutionLog.objects.filter(pk=log.pk).update(status='failed', resume_state=state)
    ExecutionTask.objects.filter(pk=task.pk).update(
        status='failed', pause_type='failed', pause_action='', resume_deadline=deadline)
    append_note(task.pk, log, f'✖ {note}。可再次重试 / 跳过 / 终止。')
    push_status(task.pk)


def append_note(task_id, log, note):
    """在一条已结束的执行末尾追加一段说明：落盘文件、任务与执行记录的输出预览、面板。

    失败待处理期间的终止/等锁超时都发生在执行之外（没有 LiveSink），要手动把
    这几处对齐，否则面板按 output_offset 拼出来的日志会错位。
    """
    from .models import ExecutionTask, TaskExecutionLog

    abs_path = Path(settings.TASK_LOG_DIR) / log_rel_path(task_id, log.pk)
    prior = _read_prior_output(abs_path, log)
    chunk = (SEP if prior else '') + note
    try:
        if abs_path.exists():
            with open(abs_path, 'a', encoding='utf-8') as fh:
                fh.write(chunk)
    except OSError:
        logger.exception('任务日志追加失败：%s', abs_path)
    output = prior + chunk
    preview = preview_of(output)
    ExecutionTask.objects.filter(pk=task_id).update(output=preview, output_offset=len(output))
    TaskExecutionLog.objects.filter(pk=log.pk).update(output=preview)
    tail = output[-PREVIEW_LIMIT:]
    push(task_id, {'kind': 'full', 'output': tail,
                   'start': len(output) - len(tail), 'offset': len(output)})


def _run_until_done(task_id, log_id):
    """thread/sync 后端：撞锁就在当前线程 sleep 后重试，直至执行完毕或等锁超时。"""
    wait_since = None
    while True:
        wait_since = execute_task(task_id, log_id, wait_since)
        # 只认 float：测试里 execute_task 常被 mock 掉，返回的 MagicMock 不能当成撞锁
        if not isinstance(wait_since, float):
            return
        time.sleep(settings.TASK_HOST_LOCK_RETRY)


def _run_in_thread(task_id, log_id):
    import threading

    def _worker():
        try:
            _run_until_done(task_id, log_id)
        finally:
            connection.close()  # 线程退出前释放数据库连接

    threading.Thread(target=_worker, daemon=True).start()


def dispatch(task, triggered_by=None, trigger_type='execute'):
    """创建本次执行的日志记录（含执行范围快照），并按配置的后端调度执行。"""
    log = create_log(task, triggered_by=triggered_by, trigger_type=trigger_type)
    schedule(task, log)
    return log


def create_log(task, triggered_by=None, trigger_type='execute'):
    """建出本次执行的 pending 日志记录（含执行范围快照），不调度。"""
    from .models import TaskExecutionLog

    return TaskExecutionLog.objects.create(
        task=task,
        trigger_type=trigger_type,
        triggered_by=triggered_by or task.created_by,
        exec_mode=task.exec_mode,
        servers_snapshot=[{'id': s.id, 'name': s.name, 'ip': s.conn_ip} for s in task.servers.all()],
    )


def schedule(task, log):
    """按配置的后端调度一条已建好的执行记录。"""
    backend = getattr(settings, 'TASK_EXEC_BACKEND', 'thread')
    if backend == 'sync':
        _run_until_done(task.id, log.id)
    elif backend == 'celery':
        run_execution_task.apply_async(args=[task.id, log.id], queue=TASK_EXEC_QUEUE)
    else:
        _run_in_thread(task.id, log.id)


# ── Celery 任务（仅在已安装 celery 时可用）─────────────────
try:
    from celery import shared_task

    @shared_task(name='task_exec.run_execution_task')
    def run_execution_task(task_id, log_id=None, wait_since=None):
        wait_since = execute_task(task_id, log_id, wait_since)
        if isinstance(wait_since, float):
            # 撞锁：重新投递而不是在 worker 里 sleep，等锁期间不占执行位
            run_execution_task.apply_async(
                args=[task_id, log_id], kwargs={'wait_since': wait_since},
                countdown=settings.TASK_HOST_LOCK_RETRY, queue=TASK_EXEC_QUEUE)

    @shared_task(name='task_exec.expire_failure_waits')
    def expire_failure_waits_task():
        # beat 定时收尾逾期的失败待处理（见 trigger.expire_failure_waits）
        from .trigger import expire_failure_waits

        return expire_failure_waits()

except ImportError:  # celery 未安装时提供占位，避免导入错误
    def run_execution_task(task_id, log_id=None, wait_since=None):  # type: ignore
        raise RuntimeError('Celery 未安装，无法使用 celery 后端。请改用 thread/sync。')

    # dispatch 走 apply_async 投队列，占位函数也得有这个属性，否则抛的是
    # AttributeError，而不是上面那句人能看懂的话
    run_execution_task.apply_async = lambda *args, **kwargs: run_execution_task()
