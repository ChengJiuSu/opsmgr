"""任务执行调度层。

支持三种后端（settings.TASK_EXEC_BACKEND）：
  - 'thread'（默认）：后台线程异步执行，无需 Redis/Celery，适合开发/小规模。
    注意 ansible 子进程与 O(n) 的日志汇聚都跑在 web 进程里，会和 API 抢 GIL 与
    CPU —— 生产用它会让后台响应随任务输出量一起变慢，故生产应选 celery。
  - 'celery'：投递到独立队列 TASK_EXEC_QUEUE，由专用 worker 消费（部署见
    deploy/ops-manager-celery-exec.conf），执行与 web 进程彻底分家。
  - 'sync'：当前请求线程内同步执行，便于测试。

每次执行（含重跑）在 TaskExecutionLog 落一条日志记录：dispatch 时创建（pending），
执行结束回填结果；任务本体字段仅作为「最近一次执行」快照。

执行期间输出由 live.LiveSink 边跑边写：数据库存尾部预览（前端轮询实时刷新），
完整日志流式落盘到 TASK_LOG_DIR/<task_id>/<log_id>.log（按保留期定期清理）。
"""
import logging
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.utils import timezone

from . import runner
from .live import DB_PREVIEW_LIMIT, PREVIEW_LIMIT, LiveSink, log_rel_path, preview_of, push, push_status

logger = logging.getLogger('apps.task_exec')

# 数据库内输出预览上限（字符），完整输出见落盘文件
OUTPUT_PREVIEW_LIMIT = DB_PREVIEW_LIMIT

# 任务执行专用队列。不与证书签发/分发（默认 celery 队列）共用：acme.sh 的 DNS
# 验证上限 900s，一个正在签发的证书会把后面排队的 ansible 任务堵十几分钟，而
# 页面上只是一条「执行中」且无报错的任务。消费者的 -Q 必须与此一致。
TASK_EXEC_QUEUE = 'task_exec'


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
    push_status(task.pk)  # 开跑：推送 running（WebSocket 客户端已在等）

    rel_path = log_rel_path(task.pk, log.pk)
    abs_path = Path(settings.TASK_LOG_DIR) / rel_path
    sink = LiveSink(task.pk, log.pk, log_path=str(abs_path))

    rc, output, status, steps_result = -1, '', 'failed', []
    try:
        rc, output, steps_result = runner.run(task, sink=sink)
        output = output or ''
        status = 'success' if rc == 0 else 'failed'
        sink.close(final_output=output)
    except Exception as exc:  # noqa: BLE001 - 兜底，保证状态落库
        logger.exception('任务 %s 执行异常', task_id)
        note = f'执行异常：{exc}'
        partial = sink.render()[0]
        sink.close(final_output=partial, note=note)
        rc, output, status = -1, (f'{partial}\n\n{note}' if partial else note), 'failed'
    finally:
        finished_at = timezone.now()
        preview = preview_of(output)

        task.return_code, task.output, task.status, task.finished_at = rc, preview, status, finished_at
        task.save(update_fields=['return_code', 'output', 'status', 'finished_at'])

        log.return_code, log.output, log.status, log.finished_at = rc, preview, status, finished_at
        log.steps_result = steps_result
        # 流式落盘成功即回填路径（详情页/下载读全量日志），落盘失败退回预览
        log.log_file = rel_path if abs_path.exists() else ''
        log.save(update_fields=['return_code', 'output', 'status', 'finished_at', 'log_file', 'steps_result'])

        # 收尾推送：整段最终输出 + 终态（客户端据此停连接并刷新执行记录）。
        # 推送按实时窗口取尾部，比写库那份宽——面板上收尾后还要能往回翻几屏，
        # 而库里那份只是轮询回退与文件过期后的兜底。
        final_push = preview_of(output, PREVIEW_LIMIT)
        push(task.pk, {'kind': 'full', 'output': final_push, 'offset': len(final_push)})
        push_status(task.pk)


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
        servers_snapshot=[{'id': s.id, 'name': s.name, 'ip': s.conn_ip} for s in task.servers.all()],
    )
    backend = getattr(settings, 'TASK_EXEC_BACKEND', 'thread')
    if backend == 'sync':
        execute_task(task.id, log.id)
    elif backend == 'celery':
        run_execution_task.apply_async(args=[task.id, log.id], queue=TASK_EXEC_QUEUE)
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

    # dispatch 走 apply_async 投队列，占位函数也得有这个属性，否则抛的是
    # AttributeError，而不是上面那句人能看懂的话
    run_execution_task.apply_async = lambda *args, **kwargs: run_execution_task()
