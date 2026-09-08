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

# ── 两个预览上限（刻意拆开，口径不同）────────────────────
# PREVIEW_LIMIT：**实时窗口**。实时块保留的尾部长度（_trim）、WebSocket 整段
#   重发（'kind': 'full'）的体积上限。并行组每次 flush 都要整段重发（多块交错
#   无法当增量发），所以这个值直接决定推送带宽：0.5s 推一次全窗口，60000 就是
#   120KB/s 经 Redis 再进 WebSocket。前端收 delta 时本地是追加的，本地可见长度
#   不受此限，只有整段重发才回落到这个窗口。
# DB_PREVIEW_LIMIT：**写库上限**。落进 ExecutionTask.output /
#   TaskExecutionLog.output 的尾部长度。它只是「WebSocket 不可用时的轮询回退」
#   与「日志文件过保留期后的残留兜底」，不是主展示通道——全量日志在
#   TASK_LOG_DIR 的落盘文件里。原来与实时窗口共用 60000，等于每秒往 MySQL 写
#   一次 60KB TEXT（binlog 跟着 60KB/s），而页面根本不读它。
PREVIEW_LIMIT = 16000
DB_PREVIEW_LIMIT = 8000

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
# 写库贵（一次最多 DB_PREVIEW_LIMIT 字符的 TEXT 更新），而它只是 WebSocket
# 不可用时的回退通道（前端轮询间隔 1s），没必要跟着推送的频率走。
# 合成一个节流时，要么写库跟着推送变频繁，要么推送跟着写库变迟钝，两头都不对。
# DB_INTERVAL 取 3s 而非 1s：轮询回退端每秒拉一次，最坏看到 3s 前的尾部，而
# 回退本身已是降级通道；换来的是写库量再降 2/3。
PUSH_INTERVAL = 0.5
PUSH_BYTES = 2048
DB_INTERVAL = 3.0
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


def preview_of(text, limit=DB_PREVIEW_LIMIT):
    """取尾部预览：超限时截断并标注，避免前端拿到超大字符串。

    收尾路径（tasks.execute_task）用它——那里本来就有完整输出在手。执行中的
    flush 走 _preview，避免为了取尾部而先拼出一个几 MB 的完整串。

    limit 默认写库口径（DB_PREVIEW_LIMIT）；收尾推送传 PREVIEW_LIMIT，让面板
    留住更长的尾部。
    """
    text = text or ''
    if len(text) <= limit:
        return text
    return TRUNCATE_NOTE + text[-limit:]


def _preview(total, tail, limit=DB_PREVIEW_LIMIT):
    """由（完整输出总长度、尾部片段）组出预览，与 preview_of 同口径。

    tail 是 join_tail(parts, PREVIEW_LIMIT) 的结果（实时窗口），写库时要再收窄
    到 limit —— 再切一次只有 O(limit)，比为了写库单独拼一次尾部便宜。
    """
    if total <= limit:
        return tail
    return TRUNCATE_NOTE + tail[-limit:]


def split_note(preview):
    """拆掉预览头部的截断说明，返回 (是否截断, 正文)。

    正文长度是算绝对偏移的依据（start = 总长度 - len(正文)），说明文字不能算进去。
    """
    preview = preview or ''
    if preview.startswith(TRUNCATE_NOTE):
        return True, preview[len(TRUNCATE_NOTE):]
    return False, preview


def log_rel_path(task_id, log_id):
    """执行日志落盘相对路径（相对 settings.TASK_LOG_DIR）。"""
    return str(Path(str(task_id)) / f'{log_id}.log')


# 「执行中可见日志」的重建上限（字符）。落盘文件可能几十 MB，而这份重建结果要整段
# 过 channel layer（Redis）再进 WebSocket 帧、或塞进一次轮询响应，不能按文件大小走。
# 20 万字符约几千行 ansible 输出，够往回翻很多屏；更早的内容执行结束后在本页看全量。
LIVE_WINDOW_LIMIT = 200000
# 落盘文件单次最多读这么多字节（UTF-8 中文约 3 字节/字符，够铺满上面那个窗口）
FILE_READ_LIMIT = LIVE_WINDOW_LIMIT * 4


def _tail(text, limit=LIVE_WINDOW_LIMIT):
    return text if len(text) <= limit else text[-limit:]


def read_live_output(task):
    """组出「执行中」可见的最长日志，及其绝对偏移。

    实时窗口（库内尾部预览）只有 DB_PREVIEW_LIMIT 字符，跑到第二个模块时第一个
    模块就滑出窗口了——而**已完成模块的完整输出**执行期间就已流式落盘。所以这里
    以落盘文件为「历史」、库内预览为「当前模块尾部」，两段按绝对偏移拼接：

      head   已知但不与尾段相连的前段（落盘文件内容，缺口时非空）
      output 尾段正文，覆盖绝对区间 [start, offset)
      offset 完整输出总长度（= 服务端偏移基准）

    返回的 head 为空时 output 即从 0 开始的连续全文（常见情况）。前端把
    (head, output, start) 拼成展示文本，并用 start 给后续增量帧定位。
    """
    truncated, body = split_note(task.output or '')
    total = task.output_offset or (len(body) if not truncated else 0)
    if total < len(body):        # 老数据/未回填偏移：只能按「预览即全文」处理
        total = len(body)
    start = total - len(body)
    log = task.logs.order_by('-created_at', '-id').values('pk', 'steps_result').first()
    if not start:
        return {'head': '', 'output': body, 'start': 0, 'offset': total}

    file_text, complete = _read_running_log_file(task, log)
    if file_text is None:
        return {'head': _running_headers(log), 'output': body,
                'start': start, 'offset': total}
    file_len = len(file_text)
    if complete and start <= file_len:
        # 预览与落盘部分有重叠：去掉重叠后接上，得到连续的一段（够长则从 0 开始）
        whole = file_text + body[file_len - start:]
        visible = _tail(whole)
        return {'head': '', 'output': visible,
                'start': total - len(visible), 'offset': total}
    # 当前模块自身的输出已超出实时窗口（或落盘文件本身已超出重建上限，只取到尾部
    # 因而算不出重叠位置）：前段照给，中间按缺口处理
    head = _tail(file_text)
    # 尾段是「执行中模块」的日志，但它的模块头已经滚出窗口了。把模块头补在前段
    # 末尾，尾段才会被面板归到正在执行的那个模块名下（否则算到上一个模块头上，
    # 当前模块看起来「暂无输出」，而这正是执行中途打开日志页看到的现象）
    running = _running_headers(log)
    if running:
        head = f'{head}{SEP}{running}' if head else running
    return {'head': head, 'output': body, 'start': start, 'offset': total}


def _running_headers(log):
    """正在执行的模块的模块头（由 runner 随 running 状态一起发布，见 step_status）。"""
    steps = (log or {}).get('steps_result') or []
    return SEP.join(
        item['header'] for item in steps
        if item.get('status') == 'running' and item.get('header')
    )


def _read_running_log_file(task, log):
    """读本次执行已落盘的日志（已完成模块的完整输出），返回 (文本, 是否读全)。

    执行中的 TaskExecutionLog.log_file 还没回填（收尾才写），所以按最新一条执行
    记录的 id 直接拼路径。不可读时返回 (None, False)。

    只读尾部 FILE_READ_LIMIT 字节：文件可能几十 MB，而重建结果最多也就
    LIVE_WINDOW_LIMIT 字符。读不全时字符总长度未知（拼不出重叠位置），调用方
    据 complete=False 走「前段 + 缺口」那条路。
    """
    from django.conf import settings

    log_id = (log or {}).get('pk')
    if not log_id:
        return None, False
    path = Path(settings.TASK_LOG_DIR) / log_rel_path(task.pk, log_id)
    try:
        size = path.stat().st_size
        with path.open('rb') as fh:
            if size > FILE_READ_LIMIT:
                fh.seek(size - FILE_READ_LIMIT)
                # 尾部起点可能切在一个多字节字符中间，errors='replace' 顶掉那半个字符
                return fh.read().decode('utf-8', 'replace'), False
            return fh.read().decode('utf-8', 'replace'), True
    except OSError:
        return None, False


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
        self._steps = None      # runner 的逐模块结果列表（同一对象，含未执行的 pending）
        self._extra = None      # 当前的临时提示（尾随刷新沿用）
        self._db_len = -1       # 上次写库时的输出总长度（内容没变就不重复写）
        self._io_lock = threading.Lock()   # 串行化写库/推送，避免交叉出乱序
        self._stop = threading.Event()     # close() 置位，通知尾随刷新线程退出
        self._ticker = None

    # ── 生命周期 ────────────────────────────────────────────
    def bind(self, outputs):
        """绑定 runner 的已完成模块输出列表（同一对象，后续只读）。"""
        self._outputs = outputs

    def bind_steps(self, steps):
        """绑定 runner 的逐模块结果列表（含尚未执行的 pending 项）并立刻发布一次。

        执行中就把整张模块表（含后续待执行模块）落到执行日志记录上，面板据此画出
        完整的任务流树 —— 否则执行中只能靠解析日志里已出现的模块头，跑一个才看见
        一个，且先前模块滑出实时窗口后连树上的节点都会消失。
        """
        self._steps = steps
        self.publish_steps()

    def step_status(self, index, **fields):
        """更新第 index 个模块（1 起）的状态并发布（模块起止各一次，量很小）。"""
        if not self._steps or not 1 <= index <= len(self._steps):
            return
        self._steps[index - 1].update(fields)
        self.publish_steps()

    def publish_steps(self):
        """把当前模块表写进执行日志记录并推给面板。"""
        if self._steps is None:
            return
        from .models import TaskExecutionLog

        steps = [dict(item) for item in self._steps]
        if self.log_id:
            try:
                TaskExecutionLog.objects.filter(pk=self.log_id).update(steps_result=steps)
            except Exception as exc:  # noqa: BLE001 - 写库失败不中断执行
                logger.warning('任务 %s 模块表写库失败：%s', self.task_id, exc)
        push(self.task_id, {'kind': 'steps', 'steps': steps})

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

    def _ensure_ticker(self):
        """启动尾随刷新线程：每 PUSH_INTERVAL 补一次 flush。

        存在的理由见 flush 的注释：节流挡掉的那一段没有别的触发点会来补，而
        ansible 的输出天生是「一阵一阵」的，两阵之间可能隔上几分钟。线程是
        daemon，close() 置 _stop 后退出；一次执行只有一个（并行组共用一个 sink）。
        """
        with self._lock:
            if self._ticker is not None or self._stop.is_set():
                return
            self._ticker = threading.Thread(
                target=self._tick_loop, name=f'live-flush-{self.task_id}', daemon=True)
            ticker = self._ticker
        ticker.start()

    def _tick_loop(self):
        while not self._stop.wait(PUSH_INTERVAL):
            try:
                self.flush(extra=self._extra)
            except Exception:  # noqa: BLE001 - 补推失败不影响执行本体
                logger.exception('任务 %s 实时输出补推失败', self.task_id)

    def begin(self, key, header, order=0):
        """登记一个正在执行的模块（实时块，模块结束后由 outputs 接管）。"""
        with self._lock:
            self._live[key] = self._new_block(order, header)
        self._ensure_ticker()
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
        self._ensure_ticker()   # 未经 begin 直接 feed 的兜底路径也要有尾随刷新
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
        self._stop.set()          # 先停尾随刷新，避免它和收尾写库/落盘打架
        ticker = self._ticker
        if ticker is not None and ticker is not threading.current_thread():
            ticker.join(timeout=PUSH_INTERVAL * 2)
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

        只在这里节流是不够的：flush 全靠 feed/begin/end 驱动，被节流挡掉的那一段
        没有人再来补。ansible 是「每个 task 结束吐一段」，于是一段输出落在节流窗口
        内、紧接着下一个 task 跑上几分钟（拉包、解压），这段就一直躺在内存里不推
        ——页面看着像卡住，等模块收尾的 force flush 才一次性全出来。补推交给
        _ticker 线程（见 _ensure_ticker）。
        """
        now = time.monotonic()
        with self._lock:
            self._extra = extra    # 尾随刷新要沿用当前提示，否则补推会把它抹掉
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
            # 内容没变就不写库：尾随刷新每 PUSH_INTERVAL 敲一次，不加这道判断会
            # 在无输出的长任务期间每 DB_INTERVAL 重写一遍同样的预览
            db_due = db_due and (force or total != self._db_len)
            if db_due:
                self._last_db, self._db_len = now, total
        # 写库与推送都可能阻塞（DB / channel layer），放锁外；串行化避免并行组多线程
        # 与尾随刷新交叉写出乱序的预览/推送
        with self._io_lock:
            if db_due:
                # 执行日志表只在 force（模块起止、阶段收尾）时跟着写一份：任务表
                # 才是前端轮询读的对象，两张表都按推送频率写整份预览纯属浪费。
                self._save_preview(_preview(total, tail), total, write_log=force)
            if push_due:
                self._broadcast(total, tail, extra_used=bool(extra))

    def _broadcast(self, total, tail, extra_used):
        """把新增片段推给 WebSocket 客户端（前缀被破坏时重发实时窗口）。

        total 为完整输出长度，tail 为其末尾 PREVIEW_LIMIT 字符——增量都落在尾部，
        所以从 tail 里切即可，无需持有完整输出。

        重发帧带 start（= 这段文本的绝对起点）：客户端按绝对偏移把它盖回自己那份
        更长的日志上，只替换窗口内的尾段，窗口之外已收到的模块不受影响。没有
        start 时客户端只能整份换成一个 16000 字符的窗口，先前模块当场消失——这正
        是「执行第二个模块时第一个模块的日志看不见」的由来。

        start 也可能偏大（并行组里多个实时块各自裁过头部，拼起来缺中段），客户端
        对不上就把这段接在已有内容之后并标出缺口，历史一样不丢。
        """
        if self._ws_broken:
            return
        with self._lock:
            delta_len = total - self._ws_len
            # 临时提示会消失、并行组多块交错插入、长度回退：都不再是 append，重发窗口。
            # 增量超出 tail 覆盖范围时同样重发（切不出来，且这么大的增量本就
            # 不适合当 delta 发）
            reset = (self._ws_reset or extra_used or len(self._live) > 1
                     or delta_len < 0 or delta_len > len(tail))
            chunk = '' if reset else tail[len(tail) - delta_len:]
            if not reset and not chunk:
                return
            self._ws_len, self._ws_reset = total, False
        payload = ({'kind': 'full', 'output': tail, 'start': max(0, total - len(tail)),
                    'offset': total}
                   if reset else {'kind': 'delta', 'output': chunk, 'offset': total})
        if not push(self.task_id, payload):
            self._ws_broken = True  # 通道不可用：不再重试，前端自行回退轮询

    def _save_preview(self, preview, total, write_log=True):
        """写实时预览与输出总长度。write_log=False 时只写任务表（轮询读的就是它）。

        执行日志表是「历史记录」，执行中没人读它的 output（详情接口在执行结束
        回填 log_file 后直接读落盘全量），跟着推送频率一起写等于把同一份预览
        TEXT 写两遍。

        total 一起落库（output_offset）：预览只是尾部，读的一端要靠它才能算出这段
        文本在完整输出里的位置，进而与落盘文件拼成连续日志（read_live_output）。
        """
        from .models import ExecutionTask, TaskExecutionLog

        try:
            ExecutionTask.objects.filter(pk=self.task_id).update(
                output=preview, output_offset=total)
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
