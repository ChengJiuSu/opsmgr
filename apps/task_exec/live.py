"""任务执行的实时输出汇聚（流式日志）。

runner 每个模块把 ansible 子进程输出分片喂给 LiveSink，由它负责：
  - 节流写库：ExecutionTask.output / TaskExecutionLog.output 存尾部预览，
    前端（WebSocket 不可用时）轮询即可看到「边跑边出」的日志；
  - 实时推送：经 channel layer 组消息把新增片段推给 WebSocket 客户端
    （consumers.TaskLogConsumer），无需轮询；
  - 落盘留存：完整日志按执行记录写 TASK_LOG_DIR/<task_id>/<log_id>.log。

落盘以「已完成模块」为粒度（append-only 前缀，天然去重且不会因并行组交错），
实时性由写库预览提供；两者内容一致，文件为全量、库内为尾部截断。
"""
import logging
import os
import threading
import time
from pathlib import Path

from asgiref.sync import async_to_sync

from channels.layers import get_channel_layer

logger = logging.getLogger('apps.task_exec')

# 数据库内输出预览上限（字符），与 tasks.OUTPUT_PREVIEW_LIMIT 同口径
PREVIEW_LIMIT = 60000
# 写库节流：距上次写库满 FLUSH_INTERVAL 秒、或新增满 FLUSH_BYTES 字符才落库
FLUSH_INTERVAL = 0.5
FLUSH_BYTES = 2048
TRUNCATE_NOTE = '…（日志过长，此处仅显示尾部；执行结束后在执行记录详情查看完整日志）\n\n'


def preview_of(text):
    """取尾部预览：超限时截断并标注，避免前端拿到超大字符串。"""
    text = text or ''
    if len(text) <= PREVIEW_LIMIT:
        return text
    return TRUNCATE_NOTE + text[-PREVIEW_LIMIT:]


def log_rel_path(task_id, log_id):
    """执行日志落盘相对路径（相对 settings.TASK_LOG_DIR）。"""
    return str(Path(str(task_id)) / f'{log_id}.log')


def push(task_id, payload):
    """向任务日志组推送一条消息（channel layer 不可用时静默跳过）。

    推送失败（未装/未连 Redis 等）只告警：前端在 WebSocket 不可用时自动
    回退轮询，执行本体不受影响。
    """
    from .consumers import group_name

    layer = get_channel_layer()
    if layer is None:
        return False
    try:
        async_to_sync(layer.group_send)(
            group_name(task_id), {'type': 'task.log', 'payload': payload})
        return True
    except Exception as exc:  # noqa: BLE001 - 推送失败不影响执行与落库
        logger.warning('任务 %s 日志推送失败：%s', task_id, exc)
        return False


def push_status(task_id):
    """推送任务状态变化（执行中/暂停/终态），供前端切换操作条与收尾。"""
    from .models import ExecutionTask

    task = ExecutionTask.objects.filter(pk=task_id).first()
    if task is None:
        return
    push(task_id, {
        'kind': 'status',
        'status': task.status,
        'pause_type': task.pause_type,
        'return_code': task.return_code,
        'duration': task.duration,
    })


class LiveSink:
    """一次执行的输出汇聚器（线程安全，供并行组多线程共用）。

    用法：bind(outputs) 绑定 runner 的已完成模块输出列表 → 每模块
    begin/writer/end → 阶段结束 flush(force=True) → 收尾 close()。
    """

    def __init__(self, task_id, log_id=None, log_path=None):
        self.task_id = task_id
        self.log_id = log_id
        self.log_path = log_path
        self._outputs = []      # runner 的 outputs 列表（append-only）
        self._live = {}         # key -> {'order','header','buf'}
        self._lock = threading.RLock()
        self._file_len = 0      # 已落盘的 outputs 前缀长度（字符）
        self._fh = None
        self._file_broken = False
        self._last_flush = 0.0
        self._pending = 0
        self._ws_len = 0        # 已推送的 render 文本长度（增量基准）
        self._ws_reset = False  # 下次推送整段（模块切换后文本可能被重排）
        self._ws_broken = False

    # ── 生命周期 ────────────────────────────────────────────
    def bind(self, outputs):
        """绑定 runner 的已完成模块输出列表（同一对象，后续只读）。"""
        self._outputs = outputs

    def begin(self, key, header, order=0):
        """登记一个正在执行的模块（实时块，模块结束后由 outputs 接管）。"""
        with self._lock:
            self._live[key] = {'order': order, 'header': header, 'buf': []}
        self.flush(force=True)

    def writer(self, key):
        """返回绑定到该模块的 on_data 回调。"""
        return lambda text: self.feed(key, text)

    def feed(self, key, text):
        """喂入一段输出分片（节流写库）。"""
        if not text:
            return
        with self._lock:
            entry = self._live.get(key)
            if entry is None:  # 未 begin（或已 end）时兜底为独立实时块
                entry = self._live.setdefault(key, {'order': 0, 'header': '', 'buf': []})
            entry['buf'].append(text)
            self._pending += len(text)
        self.flush()

    def end(self, key):
        """模块结束：丢弃实时块（完整输出由 runner 追加进 outputs）。"""
        with self._lock:
            self._live.pop(key, None)
            # 实时块交还 outputs 时文本可能被重排（如 agent 通道标头），
            # 下次推送整段以对齐客户端
            self._ws_reset = True

    def close(self, final_output=None, note=''):
        """收尾：把 outputs 剩余部分与附加说明（如执行异常）落盘并关闭文件。"""
        with self._lock:
            self._live.clear()
            if final_output is not None and len(final_output) > self._file_len:
                self._append_file(final_output[self._file_len:])
                self._file_len = len(final_output)
            if note:
                self._append_file(('\n\n' if self._file_len else '') + note)
            if self._fh is not None:
                try:
                    self._fh.close()
                except OSError:
                    pass
                self._fh = None

    # ── 输出组装与持久化 ────────────────────────────────────
    def render(self, extra=None):
        """组装当前完整输出：已完成模块 + 正在执行的实时块（+ 临时提示）。"""
        with self._lock:
            finished = '\n\n'.join(self._outputs)
            blocks = []
            for entry in sorted(self._live.values(), key=lambda e: e['order']):
                body = ''.join(entry['buf'])
                blocks.append(f"{entry['header']}\n{body}" if entry['header'] else body)
        parts = [p for p in [finished, *blocks, extra or ''] if p]
        return finished, '\n\n'.join(parts)

    def flush(self, extra=None, force=False):
        """把当前输出写入数据库预览，并把已完成部分补齐落盘。

        extra 为仅供实时查看的临时提示（如失败等待人工处理），不落盘。
        """
        now = time.monotonic()
        with self._lock:
            if not force and self._pending < FLUSH_BYTES and now - self._last_flush < FLUSH_INTERVAL:
                return
            self._last_flush, self._pending = now, 0
            finished, full = self.render(extra)
            if len(finished) > self._file_len:
                self._append_file(finished[self._file_len:])
                self._file_len = len(finished)
        self._save_preview(preview_of(full))
        self._broadcast(full, extra_used=bool(extra))

    def _broadcast(self, full, extra_used):
        """把新增片段推给 WebSocket 客户端（前缀被破坏时整段重发）。"""
        if self._ws_broken:
            return
        with self._lock:
            # 临时提示会消失、并行组多块交错插入、长度回退：都不再是 append，整段重发
            reset = (self._ws_reset or extra_used or len(self._live) > 1
                     or len(full) < self._ws_len)
            chunk = '' if reset else full[self._ws_len:]
            if not reset and not chunk:
                return
            self._ws_len, self._ws_reset = len(full), False
        payload = ({'kind': 'full', 'output': preview_of(full), 'offset': len(full)}
                   if reset else {'kind': 'delta', 'output': chunk, 'offset': len(full)})
        if not push(self.task_id, payload):
            self._ws_broken = True  # 通道不可用：不再重试，前端自行回退轮询

    def _save_preview(self, preview):
        from .models import ExecutionTask, TaskExecutionLog

        try:
            ExecutionTask.objects.filter(pk=self.task_id).update(output=preview)
            if self.log_id:
                TaskExecutionLog.objects.filter(pk=self.log_id).update(output=preview)
        except Exception as exc:  # noqa: BLE001 - 写库失败只丢一次实时预览，不中断执行
            # 并行组多线程 + SQLite 可能撞写锁（生产 MySQL 无此问题），只告警不打栈
            logger.warning('任务 %s 实时输出写库失败：%s', self.task_id, exc)

    def _append_file(self, text):
        """追加写落盘日志（首次写时建目录并收紧权限）。"""
        if not self.log_path or self._file_broken or not text:
            return
        try:
            if self._fh is None:
                path = Path(self.log_path)
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                self._fh = open(path, 'a', encoding='utf-8')
                os.chmod(path, 0o600)  # 输出可能含敏感回显，仅属主可读
            self._fh.write(text)
            self._fh.flush()
        except OSError:
            logger.exception('任务日志落盘失败：%s', self.log_path)
            self._file_broken = True  # 只告警一次，后续放弃落盘
