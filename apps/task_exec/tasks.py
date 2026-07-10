"""任务执行调度层。

支持三种后端（settings.TASK_EXEC_BACKEND）：
  - 'thread'（默认）：后台线程异步执行，无需 Redis/Celery，适合开发/小规模。
  - 'celery'：投递到 Celery worker，适合生产。
  - 'sync'：当前请求线程内同步执行，便于测试。

每次执行（含重跑）在 TaskExecutionLog 落一条日志记录：dispatch 时创建（pending），
执行结束回填结果；任务本体字段仅作为「最近一次执行」快照。
"""
import logging
import os
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.utils import timezone

from . import runner

logger = logging.getLogger('apps.task_exec')

# 数据库内输出预览上限（字符），超出部分全量落盘到 TASK_LOG_DIR
OUTPUT_PREVIEW_LIMIT = 60000


def _persist_full_output(log, output):
    """输出超过预览上限时全量落盘，返回相对 TASK_LOG_DIR 的路径。

    落盘失败仅记录告警，不影响任务与日志状态入库。
    """
    rel_path = Path(str(log.task_id)) / f'{log.pk}.log'
    abs_path = Path(settings.TASK_LOG_DIR) / rel_path
    try:
        abs_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        abs_path.write_text(output, encoding='utf-8')
        os.chmod(abs_path, 0o600)  # 输出可能含敏感回显，仅属主可读
        return str(rel_path)
    except OSError:
        logger.exception('任务日志 %s 落盘失败：%s', log.pk, abs_path)
        return ''


def execute_task(task_id, log_id=None):
    """加载任务并执行，同时更新任务快照与执行日志。可在线程/celery/同步上下文中调用。"""
    from .models import ExecutionTask, TaskExecutionLog

    task = ExecutionTask.objects.filter(pk=task_id).select_related('credential').first()
    if task is None:
        logger.warning('任务 %s 不存在，跳过执行', task_id)
        return

    log = TaskExecutionLog.objects.filter(pk=log_id).first() if log_id else None
    if log is None:
        # 兜底：直接调用（未经 dispatch）时也保证有日志记录
        log = TaskExecutionLog.objects.create(task=task, triggered_by=task.created_by)

    started_at = timezone.now()
    task.status = log.status = 'running'
    task.started_at = log.started_at = started_at
    task.save(update_fields=['status', 'started_at'])
    log.save(update_fields=['status', 'started_at'])

    rc, output, status, steps_result = -1, '', 'failed', []
    try:
        rc, output, steps_result = runner.run(task)
        output = output or ''
        status = 'success' if rc == 0 else 'failed'
    except Exception as exc:  # noqa: BLE001 - 兜底，保证状态落库
        logger.exception('任务 %s 执行异常', task_id)
        rc, output, status = -1, f'执行异常：{exc}', 'failed'
    finally:
        finished_at = timezone.now()
        preview = output[:OUTPUT_PREVIEW_LIMIT]

        task.return_code, task.output, task.status, task.finished_at = rc, preview, status, finished_at
        task.save(update_fields=['return_code', 'output', 'status', 'finished_at'])

        log.return_code, log.output, log.status, log.finished_at = rc, preview, status, finished_at
        log.steps_result = steps_result
        if len(output) > OUTPUT_PREVIEW_LIMIT:
            log.log_file = _persist_full_output(log, output)
        log.save(update_fields=['return_code', 'output', 'status', 'finished_at', 'log_file', 'steps_result'])


def _run_in_thread(task_id, log_id):
    import threading

    def _worker():
        try:
            execute_task(task_id, log_id)
        finally:
            connection.close()  # 线程退出前释放数据库连接

    threading.Thread(target=_worker, daemon=True).start()


def dispatch(task, triggered_by=None, trigger_type='execute'):
    """创建本次执行的日志记录（含执行范围快照），并按配置的后端调度执行。"""
    from .models import TaskExecutionLog

    log = TaskExecutionLog.objects.create(
        task=task,
        trigger_type=trigger_type,
        triggered_by=triggered_by or task.created_by,
        exec_mode=task.exec_mode,
        servers_snapshot=[{'id': s.id, 'name': s.name, 'ip': s.ip} for s in task.servers.all()],
    )
    backend = getattr(settings, 'TASK_EXEC_BACKEND', 'thread')
    if backend == 'sync':
        execute_task(task.id, log.id)
    elif backend == 'celery':
        run_execution_task.delay(task.id, log.id)
    else:
        _run_in_thread(task.id, log.id)
    return log


# ── Celery 任务（仅在已安装 celery 时可用）─────────────────
try:
    from celery import shared_task

    @shared_task(name='task_exec.run_execution_task')
    def run_execution_task(task_id, log_id=None):
        execute_task(task_id, log_id)

except ImportError:  # celery 未安装时提供占位，避免导入错误
    def run_execution_task(task_id, log_id=None):  # type: ignore
        raise RuntimeError('Celery 未安装，无法使用 celery 后端。请改用 thread/sync。')
