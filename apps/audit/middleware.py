import json
import logging

from common.request import client_ip

logger = logging.getLogger(__name__)

SKIP_PATHS = ('/api/schema', '/admin/jsi18n')
WRITE_METHODS = ('POST', 'PUT', 'PATCH', 'DELETE')

BODY_LIMIT = 2000

# 敏感字段：按键名（小写）打码，不入审计表。
# 登录密码、MFA 验证码/密钥、凭据私钥、各类 token 全部在列——审计表是给人
# 查阅的历史记录，读权限比这些秘密本身宽，明文落进去等于把秘密降级保管。
SENSITIVE_KEYS = frozenset({
    'password', 'old_password', 'new_password', 'confirm_password',
    'passwd', 'become_password', 'private_key', 'secret', 'secret_key',
    'token', 'access', 'refresh', 'api_token', 'api_key', 'apikey',
    'code', 'otp', 'mfa_code', 'recovery_code', 'crumb',
})
REDACTED = '***'
# 认证相关路径：非 JSON 体无法按键定位敏感值，整体丢弃而不是截断留半截密码
AUTH_PATHS = ('/api/auth/',)
DROPPED = '<非 JSON 请求体，已丢弃>'
NOT_RECORDED = '<multipart 请求体未记录>'


def redact(value):
    """递归按键名打码；非容器原样返回。"""
    if isinstance(value, dict):
        return {
            key: (REDACTED if str(key).lower() in SENSITIVE_KEYS else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def redact_body(raw, path):
    """请求体脱敏后截断，返回可入库的文本。"""
    if not raw:
        return ''
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        # 表单等非 JSON 体：认证路径整体丢弃，其余保留截断原文
        if path.startswith(AUTH_PATHS):
            return DROPPED
        return raw[:BODY_LIMIT]
    return json.dumps(redact(data), ensure_ascii=False)[:BODY_LIMIT]


class AuditLogMiddleware:
    """写操作审计。

    请求体必须在视图执行**之前**取：DRF 的解析器直接从原始流读，读完
    `_read_started=True`，中间件事后再取 `request.body` 只会抛
    RawPostDataException——实测线上 413 条审计里仅 9 条有 body，登录类
    114 条全空，审计等于形同虚设。这里先摸一次 `request.body` 把它缓存下来
    （Django 之后用 BytesIO(_body) 交给 DRF，解析不受影响），事后再脱敏入库。
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        recordable = (request.method in WRITE_METHODS
                      and not request.path.startswith(SKIP_PATHS))
        if recordable:
            self._preload_body(request)
        response = self.get_response(request)
        if recordable:
            self._record(request, response)
        return response

    @staticmethod
    def _preload_body(request):
        """预读并缓存请求体，multipart 除外（见 _body 说明）。"""
        if request.content_type and request.content_type.startswith('multipart/'):
            return
        try:
            request.body  # noqa: B018 - 取值即缓存到 request._body
        except Exception:  # noqa: BLE001 - 读不到就按无 body 处理
            pass

    def _record(self, request, response):
        from .models import AuditLog

        AuditLog.objects.create(
            user=request.user if request.user.is_authenticated else None,
            action=request.method,
            path=request.path,
            method=request.method,
            ip=client_ip(request) or None,
            status_code=response.status_code,
            body=redact_body(self._body(request), request.path),
        )

    @staticmethod
    def _body(request):
        """取请求体文本。

        multipart 直接跳过：制品上报会传数百 MB 的构建包，读它等于把整个包
        塞进内存换一行日志，而且存进去的是二进制片段（线上已有一条这样的
        记录），对排障毫无价值。
        """
        if request.content_type and request.content_type.startswith('multipart/'):
            return NOT_RECORDED
        try:
            return request.body.decode('utf-8', errors='replace')
        except Exception:  # noqa: BLE001 - 流式请求体读不到就不记
            return ''
