"""任务执行的实时输出汇聚（流式日志）。

runner 每个模块把 ansible 子进程输出分片喂给 LiveSink，由它负责：
  - 实时推送：经 channel layer 组消息把新增片段推给 WebSocket 客户端
    （consumers.TaskLogConsumer），无需轮询；
  - 节流写库：ExecutionTask.output 存尾部预览，供 WebSocket 不可用时的轮询回退；
  - 落盘留存：完整日志按执行记录写 TASK_LOG_DIR/<task_id>/<log_id>.log。

落盘以「已完成模块」为粒度（append-only 前缀，天然去重且不会因并行组交错），
实时性由推送与写库预览提供；三者内容一致，文件为全量、库内为尾部截断。

**为什么不在 flush 里拼完整输出**：完整输出可能几 MB，而每次 flush 只需要尾部
PREVIEW_LIMIT 字符。按 2KB 粒度反复 SEP.join(全部分段) 是 O(n²)（10MB 输出约
5000 次全量重拼，几十 GB 字符串拷贝，纯 Python 还持着 GIL，直接把所在进程的一个
核吃满）。所以热路径只走 join_len / join_tail，完整拼接留给冷路径 render()。

**为什么推送和写库分两个节流**：推送便宜且延迟敏感（亚秒级），写库贵（一次最多
PREVIEW_LIMIT 字符的 TEXT 更新）且只是轮询回退通道。合成一个节流时两头都不对。
执行日志表（TaskExecutionLog.output）更进一步，只在 force 时跟着写。

**敏感变量打码**（mask_secrets / LiveSink.add_secrets）：变量管理里标记为
is_secret 的值会被原样注入 ansible，而 debug 任务、失败回显很容易把它吐到
stdout。剧本不是我们写的，指望作者都记得加 `no_log` 不现实，所以在输出进入
库/文件/WebSocket 之前统一过滤——这是唯一不依赖别人配合的位置。
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

# 打码的最短明文长度：太短的值（"1"、"on"）在输出里到处都是，全替换会把日志
# 打成马赛克，反而看不出执行做了什么
SECRET_MASK_MIN_LEN = 4
SECRET_MASK = '***'


def mask_secrets(text, secrets):
    """把已知敏感明文替换为 ***。

    局限（刻意接受）：分片喂入时，一个明文如果正好被切在两个分片之间就漏过
    这一层。但模块结束时 runner 会对**完整输出**再整体打码一遍，且实时块在
    end() 时被丢弃换成完整输出，所以落库/落盘的最终内容一定是干净的——漏的
    只可能是正在看实时日志的那一瞬间，而那是发起执行的登录用户自己。
    """
    if not text or not secrets:
        return text
    # 长的先替换：短明文可能是长明文的子串，先换短的会把长的切成拼不回的片段
    for value in sorted(secrets, key=len, reverse=True):
        text = text.replace(value, SECRET_MASK)
    return text


# 模块间分隔符。runner.run 用 SEP.join(outputs) 组装最终输出，close() 又按长度
# 前缀比对续写落盘文件，所以这里的分隔符口径必须和它完全一致。
SEP = '\n\n'

# ── 两级节流：推送与写库分开 ────────────────────────────────
# WebSocket 推送便宜（一段增量文本进 channel layer），延迟敏感，保持亚秒级；
# 写库贵（一次最多 PREVIEW_LIMIT 字符的 TEXT 更新），而它只是 WebSocket 不可用时
# 的回退通道（前端轮询间隔 1s），没必要跟着推送的频率走。
# 合成一个节流时，要么写库跟着推送变频繁，要么推送跟着写库变迟钝，两头都不对。
PUSH_INTERVAL = 0.5
PUSH_BYTES = 2048
DB_INTERVAL = 1.0
TRUNCATE_NOTE = '…（日志过长，此处仅显示尾部；执行结束后在执行记录详情查看完整日志）\n\n'


def join_len(parts):
    """parts 以 SEP 连接后的总长度（不实际拼接，代价与总字符数无关）。"""
    if not parts:
        return 0
    return sum(len(p) for p in parts) + len(SEP) * (len(parts) - 1)


def join_tail(parts, limit):
    """parts 以 SEP 连接后的末尾 limit 个字符，从后往前只取够数就停。

    存在的理由：完整输出可能几 MB，而每次 flush 只需要尾部预览。整体
    SEP.join(parts) 再切尾是 O(总长度)，按 2KB 粒度反复调用就成了 O(n²)——
    10MB 输出约 5000 次全量重拼，几十 GB 字符串拷贝，纯 Python 还持着 GIL。
    这里的代价只有 O(limit)：s[-n:] 只复制 n 个字符，与 s 本身多长无关。
    """
    if limit <= 0:
        return ''
    chunks, size = [], 0          # chunks 逆序收集，最后整体反转再拼
    for part in reversed(parts):
        if size >= limit:
            break
        if chunks:                # 本段与已收集的后半之间的分隔符
            keep = min(len(SEP), limit - size)
            chunks.append(SEP[len(SEP) - keep:])
            size += keep
            if size >= limit:
                break
        room = limit - size
        chunks.append(part if len(part) <= room else part[-room:])
        size += min(len(part), room)
    return ''.join(reversed(chunks))


def preview_of(text):
    """取尾部预览：超限时截断并标注，避免前端拿到超大字符串。

    收尾路径（tasks.execute_task）用它——那里本来就有完整输出在手。执行中的
    flush 走 _preview，避免为了取尾部而先拼出一个几 MB 的完整串。
    """
    text = text or ''
    if len(text) <= PREVIEW_LIMIT:
        return text
    return TRUNCATE_NOTE + text[-PREVIEW_LIMIT:]


def _preview(total, tail):
    """由（完整输出总长度、末尾 PREVIEW_LIMIT 字符）组出预览，与 preview_of 同口径。"""
    return TRUNCATE_NOTE + tail if total > PREVIEW_LIMIT else tail


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
        # 本次执行涉及的敏感变量明文（累积）：runner 每个模块开跑前登记。
        # 累积而非按模块清空——并行组多模块共用一个 sink，跨模块多打码无害
        self._secrets = set()
        self._outputs = []      # runner 的 outputs 列表（append-only）
        self._live = {}         # key -> {'order','header','buf'}
        self._lock = threading.RLock()
        self._file_len = 0      # 已落盘的 outputs 前缀长度（字符）
        self._fh = None
        self._file_broken = False
        self._last_push = 0.0   # 上次 WebSocket 推送时刻（monotonic）
        self._last_db = 0.0     # 上次写库时刻（monotonic），比推送慢一档
        self._pending = 0
        self._ws_len = 0        # 已推送的完整输出长度（增量基准）
        self._ws_reset = False  # 下次推送整段（模块切换后文本可能被重排）
        self._ws_broken = False

    # ── 生命周期 ────────────────────────────────────────────
    def bind(self, outputs):
        """绑定 runner 的已完成模块输出列表（同一对象，后续只读）。"""
        self._outputs = outputs

    @staticmethod
    def _new_block(order=0, header=''):
        """新建实时块。size 为累计喂入量，dropped 为已丢弃的头部长度（见 _trim）。"""
        return {'order': order, 'header': header, 'buf': [], 'size': 0, 'dropped': 0}

    @staticmethod
    def _trim(entry):
        """实时块只保留尾部 PREVIEW_LIMIT 字符，丢掉的长度记进 dropped。

        实时块唯一的用途是预览与推送的**尾部**：模块结束时它整块被丢弃，完整
        输出由 runner 追加进 outputs、并由落盘文件保全。留着全量只会让每次
        flush 的 ''.join(buf) 重新变成 O(n)，把 join_tail 省下的 O(n²) 又赚回去
        —— 一个产出 10MB 的模块实测就是几十秒纯字符串拷贝。
        """
        # 条件是「丢掉这片之后仍然够 PREVIEW_LIMIT」，不是「现在超了就丢」：
        # 后者会把保留量丢到上限以下（如 60500 丢掉 3000 片只剩 57500），尾部预览
        # 就凑不满 60000 字符，只能去前面的模块里补——预览内容随即出错。
        while (len(entry['buf']) > 1
               and entry['size'] - entry['dropped'] - len(entry['buf'][0]) >= PREVIEW_LIMIT):
            entry['dropped'] += len(entry['buf'].pop(0))

    def begin(self, key, header, order=0):
        """登记一个正在执行的模块（实时块，模块结束后由 outputs 接管）。"""
        with self._lock:
            self._live[key] = self._new_block(order, header)
        self.flush(force=True)

    def writer(self, key):
        """返回绑定到该模块的 on_data 回调。"""
        return lambda text: self.feed(key, text)

    def add_secrets(self, values):
        """登记需要在输出里打码的明文（runner 按模块生效变量调用）。"""
        if not values:
            return
        with self._lock:
            self._secrets.update(values)

    def feed(self, key, text):
        """喂入一段输出分片（节流写库），敏感明文先打码。"""
        if not text:
            return
        text = mask_secrets(text, self._secrets)
        with self._lock:
            entry = self._live.get(key)
            if entry is None:  # 未 begin（或已 end）时兜底为独立实时块
                entry = self._live.setdefault(key, self._new_block())
            entry['buf'].append(text)
            entry['size'] += len(text)
            self._trim(entry)
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
                self._append_file((SEP if self._file_len else '') + note)
            if self._fh is not None:
                try:
                    self._fh.close()
                except OSError:
                    pass
                self._fh = None

    # ── 输出组装与持久化 ────────────────────────────────────
    def _parts(self, extra=None):
        """当前输出的分段列表（以 SEP 连接即为完整输出）。须在锁内调用。

        已完成模块**不做空串过滤**：_file_len / close(final_output) 都按
        SEP.join(outputs) 的长度做前缀比对，过滤会让长度对不上、落盘错位。
        （runner 写进 outputs 的每段都带 ═══ 标头，实际不会为空。）

        注意实时块的正文可能已被 _trim 裁掉头部，所以这里拼出来的**不是**完整
        输出——真实总长度要加上 _dropped()。够用的理由是它只服务尾部预览：
        每块至少留 PREVIEW_LIMIT 字符，而需要的尾部最多就 PREVIEW_LIMIT 字符。
        """
        parts = list(self._outputs)
        for entry in sorted(self._live.values(), key=lambda e: e['order']):
            body = ''.join(entry['buf'])
            block = f"{entry['header']}\n{body}" if entry['header'] else body
            if block:
                parts.append(block)
        if extra:
            parts.append(extra)
        return parts

    def _dropped(self):
        """各实时块被 _trim 丢弃的字符总数。须在锁内调用。"""
        return sum(entry['dropped'] for entry in self._live.values())

    def render(self, extra=None):
        """组装当前输出：已完成模块 + 正在执行的实时块（+ 临时提示）。

        冷路径专用（执行异常收尾时取 partial）。flush() 不走这里——它只要尾部，
        走 join_tail，理由见该函数注释。

        返回的第一个值 finished（已完成模块）是完整的，调用方
        tasks.execute_task 的异常分支取的就是它；第二个值里的实时块正文可能
        已被 _trim 裁掉头部。
        """
        with self._lock:
            finished = SEP.join(self._outputs)
            full = SEP.join(self._parts(extra))
        return finished, full

    def flush(self, extra=None, force=False):
        """把当前输出推给 WebSocket / 写入数据库预览，并把已完成部分补齐落盘。

        extra 为仅供实时查看的临时提示（如失败等待人工处理），不落盘。
        推送与写库各自节流（见 PUSH_INTERVAL / DB_INTERVAL），force 时两者都做。
        """
        now = time.monotonic()
        with self._lock:
            push_due = (force or self._pending >= PUSH_BYTES
                        or now - self._last_push >= PUSH_INTERVAL)
            db_due = force or now - self._last_db >= DB_INTERVAL
            if not push_due and not db_due:
                return
            # 落盘：只在有新完成模块时才动，与节流无关（增量、便宜）
            finished_len = join_len(self._outputs)
            if finished_len > self._file_len:
                # 取未落盘的那段尾部 = SEP.join(outputs)[self._file_len:]
                self._append_file(join_tail(self._outputs, finished_len - self._file_len))
                self._file_len = finished_len
            parts = self._parts(extra)
            # parts 里的实时块可能已被裁头，真实总长度要补回丢弃量
            total = join_len(parts) + self._dropped()
            tail = join_tail(parts, PREVIEW_LIMIT)
            if push_due:
                self._last_push, self._pending = now, 0
            if db_due:
                self._last_db = now
        # 写库与推送都可能阻塞（DB / channel layer），放锁外
        if db_due:
            # 执行日志表只在 force（模块起止、阶段收尾）时跟着写一份：任务表
            # 才是前端轮询读的对象，两张表都按推送频率写 60KB TEXT 纯属浪费。
            self._save_preview(_preview(total, tail), write_log=force)
        if push_due:
            self._broadcast(total, tail, extra_used=bool(extra))

    def _broadcast(self, total, tail, extra_used):
        """把新增片段推给 WebSocket 客户端（前缀被破坏时整段重发）。

        total 为完整输出长度，tail 为其末尾 PREVIEW_LIMIT 字符——增量都落在尾部，
        所以从 tail 里切即可，无需持有完整输出。
        """
        if self._ws_broken:
            return
        with self._lock:
            delta_len = total - self._ws_len
            # 临时提示会消失、并行组多块交错插入、长度回退：都不再是 append，整段重发。
            # 增量超出 tail 覆盖范围时同样整段重发（切不出来，且这么大的增量本就
            # 不适合当 delta 发）
            reset = (self._ws_reset or extra_used or len(self._live) > 1
                     or delta_len < 0 or delta_len > len(tail))
            chunk = '' if reset else tail[len(tail) - delta_len:]
            if not reset and not chunk:
                return
            self._ws_len, self._ws_reset = total, False
        payload = ({'kind': 'full', 'output': _preview(total, tail), 'offset': total}
                   if reset else {'kind': 'delta', 'output': chunk, 'offset': total})
        if not push(self.task_id, payload):
            self._ws_broken = True  # 通道不可用：不再重试，前端自行回退轮询

    def _save_preview(self, preview, write_log=True):
        """写实时预览。write_log=False 时只写任务表（前端轮询读的就是它）。

        执行日志表是「历史记录」，执行中没人读它的 output（详情接口在执行结束
        回填 log_file 后直接读落盘全量），跟着推送频率一起写等于把同一份 60KB
        TEXT 写两遍。
        """
        from .models import ExecutionTask, TaskExecutionLog

        try:
            ExecutionTask.objects.filter(pk=self.task_id).update(output=preview)
            if write_log and self.log_id:
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
