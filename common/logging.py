"""结构化日志：JSON 行格式 + 贯穿一次请求的 request_id。

为什么要换掉 `{levelname} {asctime} {name} ...` 的文本格式：

1. **多行**。traceback 在文本格式下是几十行独立的行，采集端（Vector）必须靠
   「下一行是否以时间戳开头」猜边界，猜错就把半截栈丢进另一条日志。JSON 格式
   把 traceback 收进 ``exc`` 字段，一条异常永远是一行，边界问题从源头消失。
2. **无时区**。原 datefmt 输出 `2026-09-17 14:10:05`，跨时区的服务端（日志库
   按 UTC 存）只能靠约定猜，猜错就是 8 小时的偏移。这里统一 RFC3339 + Z。
3. **不可查询**。VictoriaLogs 按字段建索引，`level:ERROR AND task_id:12` 这种
   查询要求 level/task_id 是字段而不是一行文本里的子串。

字段口径（与 docs/log-collection-design.md 对齐，改这里要同步改那里）：

- ``ts`` / ``msg`` —— 采集端映射为 VictoriaLogs 保留字段 ``_time`` / ``_msg``；
- ``service`` —— 本进程身份（django / celery / celery-exec / ws / feishu-ws），
  由 LOG_SERVICE 环境变量给，一台机上多个进程写各自的文件，靠它区分；
- ``host`` / ``app`` / ``env`` —— **不在这里出**，由 Vector 统一附加。进程自己
  不知道自己属于哪个环境，读环境变量等于把部署信息散进代码；Vector 的配置本来
  就是按机器分发的，放那边只有一处。

``extra=`` 传入的键原样进 JSON，于是 ``logger.info('执行失败', extra={'task_id': 12})``
在服务端就是一个可查询字段，不需要再去正则抠消息文本。
"""
import contextvars
import json
import logging
import re
import uuid
from datetime import datetime, timezone

from asgiref.sync import iscoroutinefunction, markcoroutinefunction

# LogRecord 的内置属性：这些要么已经显式取用（msg/levelname/...），要么对排障
# 无价值（relativeCreated/args/...）。凡不在此列的属性都是调用方通过 extra=
# 塞进来的业务字段，原样输出。
# 用「排除法」而不是「白名单 extra_fields」的理由：后者要求每个调用点改写成
# extra={'extra_fields': {...}}，而 extra= 是 logging 的标准用法，三方库
# （celery/django）也在用，排除法能把它们的字段一并收下。
_RECORD_ATTRS = frozenset({
    'args', 'asctime', 'created', 'exc_info', 'exc_text', 'filename',
    'funcName', 'levelname', 'levelno', 'lineno', 'module', 'msecs',
    'message', 'msg', 'name', 'pathname', 'process', 'processName',
    'relativeCreated', 'stack_info', 'thread', 'threadName', 'taskName',
})

# 请求 ID 的合法形状：nginx 的 $request_id 是 32 位十六进制，这里放宽到常见的
# UUID / ULID 写法即可。长度封顶 64。
REQUEST_ID_RE = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
REQUEST_ID_HEADER = 'HTTP_X_REQUEST_ID'
REQUEST_ID_RESPONSE_HEADER = 'X-Request-ID'

# contextvar 而非 threading.local：gunicorn 跑 gthread（线程）、daphne 跑 asyncio
# （协程），threading.local 在协程下会串味——同一线程里多个协程共用一份。
# contextvar 两种模型都正确，且 Django 的 ASGI 路径本来就靠它传 request 上下文。
_request_id = contextvars.ContextVar('request_id', default='')


def current_request_id():
    """当前请求的 ID；不在请求上下文中（celery、管理命令）时为空串。"""
    return _request_id.get()


def set_request_id(value):
    """设置当前上下文的请求 ID，返回 token 供 reset。"""
    return _request_id.set(value)


def reset_request_id(token):
    _request_id.reset(token)


def new_request_id():
    return uuid.uuid4().hex


def sanitize_request_id(value):
    """只接受形状合法的请求 ID，其余一律丢弃改用新生成的。

    上游 nginx 用 `proxy_set_header X-Request-ID $request_id` 覆盖客户端同名头，
    正常链路里到不了这里。但 Django 也可能被直连（本地调试、内网旁路），
    这时该头是客户端可控输入：不校验就等于让外部往日志字段里写任意内容
    （超长串撑爆存储、控制字符污染下游解析）。校验失败不报错、直接换一个新的，
    请求本身不受影响。
    """
    value = (value or '').strip()
    return value if REQUEST_ID_RE.match(value) else ''


class JSONFormatter(logging.Formatter):
    """把 LogRecord 渲染成单行 JSON。

    service 由 LOGGING 配置注入（见 settings/base.py），默认 django。
    """

    def __init__(self, service='django', **kwargs):
        super().__init__(**kwargs)
        self.service = service

    def format(self, record):
        created = datetime.fromtimestamp(record.created, tz=timezone.utc)
        payload = {
            # 毫秒够用：再细的精度在跨机器比对时被 NTP 误差淹没
            'ts': created.strftime('%Y-%m-%dT%H:%M:%S.') + f'{created.microsecond // 1000:03d}Z',
            'level': record.levelname,
            'service': self.service,
            'logger': record.name,
            'module': record.module,
            'line': record.lineno,
            'msg': record.getMessage(),
        }
        request_id = current_request_id()
        if request_id:
            payload['request_id'] = request_id
        if record.exc_info:
            # 关键：traceback 进字段而不是换行拼在 msg 后面，一条异常仍是一行
            payload['exc'] = self.formatException(record.exc_info)
        if record.stack_info:
            payload['stack'] = self.formatStack(record.stack_info)
        for key, value in record.__dict__.items():
            if key not in _RECORD_ATTRS and not key.startswith('_'):
                payload[key] = value
        # default=str：extra 里塞 datetime / Model / Exception 是常态，
        # 一个不可序列化的值不该让这条日志整个丢失。
        # ensure_ascii=False：中文日志占位由 3 倍降回 1 倍，且服务端直接可读。
        # separators 去掉默认的空格：每条日志省十几字节，按日均百万条算不是小数。
        return json.dumps(payload, ensure_ascii=False, default=str,
                          separators=(',', ':'))


class RequestIDMiddleware:
    """为每个请求分配 ID，写进日志上下文与响应头。

    取值优先级：上游 nginx 传来的 X-Request-ID（形状合法时）> 新生成。
    nginx access log 里记同一个值，于是「这条 502 对应后端哪几行日志」不再靠
    时间戳附近瞎找，直接 `request_id:xxx` 一把捞出 nginx + Django 两侧记录。

    必须放在 MIDDLEWARE 最前：它之后的中间件（认证、审计）产生的日志才带得上 ID。

    同步/异步双模：gunicorn 走同步路径，daphne 的 ASGI HTTP 请求走异步路径。
    不声明 async_capable 的话 Django 会把整条链路用 sync_to_async 包进线程池，
    白白多一次线程切换。（`/ws/` 的 WebSocket 连接由 Channels consumer 处理，
    根本不过 Django 中间件，所以 WS 侧的日志不带 request_id——预期如此。）
    """

    sync_capable = True
    async_capable = True

    def __init__(self, get_response):
        self.get_response = get_response
        self.async_mode = iscoroutinefunction(get_response)
        if self.async_mode:
            markcoroutinefunction(self)

    def __call__(self, request):
        if self.async_mode:
            return self.__acall__(request)
        token = self._enter(request)
        try:
            response = self.get_response(request)
        finally:
            reset_request_id(token)
        return self._exit(request, response)

    async def __acall__(self, request):
        token = self._enter(request)
        try:
            response = await self.get_response(request)
        finally:
            reset_request_id(token)
        return self._exit(request, response)

    @staticmethod
    def _enter(request):
        request_id = (sanitize_request_id(request.META.get(REQUEST_ID_HEADER))
                      or new_request_id())
        request.request_id = request_id
        return set_request_id(request_id)

    @staticmethod
    def _exit(request, response):
        # reset 放在 finally（见上）而不是这里：视图抛异常时也必须还原。
        # gthread 下线程是复用的，不还原会让下一个请求继承上一个的 ID——
        # 请求间串号比没有 ID 更糟，排障会追到完全无关的链路上。
        response[REQUEST_ID_RESPONSE_HEADER] = request.request_id
        return response
