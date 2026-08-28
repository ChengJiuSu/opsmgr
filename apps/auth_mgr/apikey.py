"""机读接口的访问控制链：API 凭证 + IP 白名单 + 作用域 + Webhook 签名。

给内部脚本与第三方 Webhook 用。它们没有浏览器、不做 MFA、也不该拿一条能干
所有事的 JWT，所以走独立的一套：

    ApiKeyAuthentication        —— 认证 + IP 白名单 + 验签（缺一不可）
    scope_required('xxx:yyy')  —— 授权，按接口切分泄露面
    ApiKeyThrottle             —— 限速，key 泄露后最后一道拦

**IP 白名单与验签放在认证类里，不放权限类**：项目里几乎每个 ViewSet 都自己
写了 permission_classes，把来源校验放权限层，新增一个机读视图时漏挂一个类
就等于全开。认证类是「用这把 key 就必过」的位置，漏不掉。

与 common/sudo.py 的 fail-closed 取向一致：任何一步判不出来（缓存异常除外，
见下）都拒绝，而不是放行。

用法（视图侧）：

    class SomeMachineView(APIView):
        authentication_classes = [ApiKeyAuthentication]
        permission_classes = [scope_required('task:trigger')]
        throttle_classes = [ApiKeyThrottle]
"""
import hashlib
import hmac
import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied, Throttled
from rest_framework.permissions import SAFE_METHODS, BasePermission
from rest_framework.throttling import SimpleRateThrottle

from common import ratelimit
from common.crypto import DecryptError
from common.request import auth_client_ip

from .models import ApiCredential

logger = logging.getLogger(__name__)

#: 可授权的作用域：`<模块>:<动作>`。值是页面上展示的说明。
#: 新增机读接口时在这里登记，scope_required 会校验拼写（拼错启动即报错）。
SCOPES = {
    'artifact:report': '上报构建制品（CI 用）',
    'artifact:download': '下载构建制品',
    'task:trigger': '触发任务流执行（内部脚本 / Webhook）',
    'asset:read': '读取主机清单（只读）',
    'variable:read': '读取执行变量（不含敏感值）',
    'variable:secret': '读取敏感变量真值（高敏，必须配 IP 白名单）',
}

#: 高敏作用域：这些凭证禁止「不限制来源 IP」（序列化器强制），
#: 因为它们能拿到密码类内容，泄露后果与其余作用域不在一个量级
HIGH_RISK_SCOPES = frozenset({'variable:secret'})

KEY_HEADER = 'X-API-KEY'
#: 兼容 `Authorization: ApiKey <明文>`，方便某些只能设 Authorization 的客户端
AUTH_KEYWORD = 'apikey'

#: 凭证校验失败的限速分桶名（common/ratelimit.py）
FAIL_SCOPE = 'api_key'

#: 服务账号的部门标记，便于在用户管理里一眼认出「这不是人」
SERVICE_DEPARTMENT = 'API 服务账号'


# ── 异常（带 error_code，调用方与前端据此区分原因）──────────
class ApiKeyInvalid(AuthenticationFailed):
    default_detail = 'API Key 校验失败。'
    default_code = 'api_key_invalid'


class ApiKeyExpired(AuthenticationFailed):
    default_detail = 'API 凭证已过期。'
    default_code = 'api_key_expired'


class ApiKeyIpDenied(PermissionDenied):
    default_detail = '来源 IP 不在该凭证的白名单内。'
    default_code = 'api_key_ip_denied'


class ApiKeySignatureInvalid(PermissionDenied):
    default_detail = 'Webhook 签名校验失败。'
    default_code = 'api_key_signature_invalid'


class ApiKeyScopeDenied(PermissionDenied):
    default_detail = '该凭证未被授权调用此接口。'
    default_code = 'api_key_scope_denied'


# ── 认证 ──────────────────────────────────────────────────
class ApiKeyAuthentication(BaseAuthentication):
    """X-API-KEY 认证：定位凭证 → 比对 secret → 过期 → IP → 验签。

    请求头里不是本系统 key 格式（`ak_` 开头）时返回 None，把请求交给下一个
    认证方式（同一个视图上可能还挂着 JWT 认证类）——请求也可能压根没带 key。

    认证成功返回 `(凭证绑定的服务账号, 凭证对象)`。返回真实 User 而不是自造的
    匿名身份，是为了让审计中间件（按 request.user 落 FK）与既有权限类原样生效。
    """

    def authenticate(self, request):
        key_id, secret = ApiCredential.split_key(self._raw_key(request))
        if not key_id:
            return None

        blocked = ratelimit.blocked_seconds(FAIL_SCOPE, key_id)
        if blocked:
            raise Throttled(wait=blocked, detail=f'该凭证校验失败次数过多，请 {blocked} 秒后重试。')

        credential = (ApiCredential.objects
                      .select_related('bound_user')
                      .filter(key_id=key_id).first())
        # 「凭证不存在」与「secret 不对」返回同一个错误：否则枚举 key_id 就能
        # 探出哪些凭证存在
        if credential is None or not credential.verify_secret(secret):
            self._record_failure(key_id)
            raise ApiKeyInvalid()

        if not credential.enabled:
            raise ApiKeyInvalid('API 凭证已停用。')
        if credential.is_expired():
            raise ApiKeyExpired()

        user = credential.bound_user
        if not (user and user.is_active):
            logger.warning('API 凭证 %s 的服务账号不可用，拒绝', credential.key_id)
            raise ApiKeyInvalid('凭证绑定的服务账号已禁用。')

        ip = auth_client_ip(request)
        if not credential.ip_allowed(ip):
            logger.warning('API 凭证 %s 来源 IP 被拒：%s', credential.key_id, ip or '(空)')
            raise ApiKeyIpDenied(f'来源 IP {ip or "未知"} 不在凭证「{credential.name}」的白名单内。')

        verify_signature(credential, request)

        ratelimit.reset(FAIL_SCOPE, key_id)
        touch(credential, ip)
        return user, credential

    def authenticate_header(self, request):
        """有这个头 DRF 才把「未认证」判成 401 而不是 403。"""
        return 'ApiKey'

    @staticmethod
    def _raw_key(request):
        raw = request.headers.get(KEY_HEADER, '')
        if raw:
            return raw
        parts = request.headers.get('Authorization', '').split()
        if len(parts) == 2 and parts[0].lower() == AUTH_KEYWORD:
            return parts[1]
        return ''

    @staticmethod
    def _record_failure(key_id):
        ratelimit.record_failure(
            FAIL_SCOPE, key_id,
            limit=settings.API_KEY_FAIL_LIMIT,
            window=settings.API_KEY_FAIL_WINDOW,
            block=settings.API_KEY_FAIL_BLOCK,
        )


def touch(credential, ip):
    """按 API_KEY_TOUCH_INTERVAL 节流地更新「最近使用」。

    每个请求写一次库会把机读接口的写压力翻倍（制品上报本来就一次一个大文件），
    而 last_used 只用于「这把 key 还有人用吗」的判断，分钟级误差无妨。
    缓存不可用时**跳过写库**而不是每次都写：宁可痕迹粗一点，也不要在 Redis
    故障时给库加一倍写。
    """
    interval = getattr(settings, 'API_KEY_TOUCH_INTERVAL', 60)
    cache_key = f'apikey:touch:{credential.key_id}'
    try:
        if not cache.add(cache_key, 1, timeout=interval):
            return
    except Exception:  # noqa: BLE001 - 缓存后端异常类型由实现决定
        logger.warning('凭证使用痕迹节流缓存不可用，本次跳过更新', exc_info=True)
        return
    try:
        credential.touch(ip)
    except Exception:  # noqa: BLE001 - 痕迹写失败绝不能影响业务调用
        logger.warning('凭证使用痕迹更新失败：%s', credential.key_id, exc_info=True)


# ── Webhook 签名 ──────────────────────────────────────────
def verify_signature(credential, request):
    """按凭证配置校验签名；未配置（sig_mode=none）直接返回。

    两种模式覆盖常见 Webhook 提供方：

    - `hmac_sha256`：HMAC-SHA256(密钥, 原始请求体) 的 hex，与请求头比对。
      GitHub 的 `X-Hub-Signature-256: sha256=<hex>`、Gitee 同构，前缀写在
      sig_prefix 里。**签的是原始字节**，不能拿反序列化再 dump 的结果算——
      空格与键序一变签名就不一致。
    - `token`：请求头里就是共享明文，恒定时间比对。GitLab 的 `X-Gitlab-Token`
      属于这类。安全性弱于 HMAC（重放与中间人拿到即可复用），只在提供方不支持
      HMAC 时用，且必须叠加 IP 白名单。
    """
    if credential.sig_mode == ApiCredential.SIG_NONE:
        return
    if not credential.sig_header:
        raise ApiKeySignatureInvalid('凭证配置了签名校验但未指定签名请求头，请检查配置。')

    provided = request.headers.get(credential.sig_header, '')
    if not provided:
        raise ApiKeySignatureInvalid(f'缺少签名请求头 {credential.sig_header}。')

    try:
        secret = credential.get_signing_secret()
    except DecryptError:
        logger.error('凭证 %s 的签名密钥解不开（MFA_SECRET_KEY/SECRET_KEY 变更？）',
                     credential.key_id)
        raise ApiKeySignatureInvalid('凭证的签名密钥无法解密，请重新设置。') from None
    if not secret:
        raise ApiKeySignatureInvalid('凭证未设置签名密钥，请先在后台配置。')

    if credential.sig_mode == ApiCredential.SIG_TOKEN:
        matched = hmac.compare_digest(provided.strip(), secret)
    else:
        matched = hmac.compare_digest(
            _strip_prefix(provided.strip(), credential.sig_prefix).lower(),
            hmac.new(secret.encode('utf-8'), _raw_body(request), hashlib.sha256).hexdigest(),
        )
    if not matched:
        logger.warning('凭证 %s 签名校验失败', credential.key_id)
        raise ApiKeySignatureInvalid()


def _strip_prefix(value, prefix):
    return value[len(prefix):] if prefix and value.startswith(prefix) else value


def _raw_body(request):
    """原始请求体字节。

    multipart 直接拒：审计中间件为了不把几百 MB 的构建包读进内存，刻意跳过
    multipart 的预读，这里也拿不到完整字节；而 multipart 本来就不是 Webhook
    的形态。制品上报那类接口用「key + IP 白名单」，不叠签名。
    """
    content_type = request.content_type or ''
    if content_type.startswith('multipart/'):
        raise ApiKeySignatureInvalid('multipart 请求不支持签名校验，请改用 IP 白名单。')
    try:
        return request.body or b''
    except Exception:  # noqa: BLE001 - 流式体读不到时按校验失败处理（fail-closed）
        logger.warning('签名校验取不到原始请求体', exc_info=True)
        raise ApiKeySignatureInvalid('无法读取请求体，签名校验失败。') from None


# ── 授权 ──────────────────────────────────────────────────
class HasApiScope(BasePermission):
    """要求凭证具备指定作用域。用 scope_required() 生成子类，不直接用。"""

    #: 子类覆盖：需要的作用域元组（全部具备才放行）
    scopes = ()
    #: 子类覆盖：True 表示该作用域只放行安全方法
    read_only = False

    def has_permission(self, request, view):
        credential = getattr(request, 'auth', None)
        if not isinstance(credential, ApiCredential):
            # 不是凭证认证进来的（未带 key / 走 JWT）：交给 401 或其他权限类判
            return False
        if self.read_only and request.method not in SAFE_METHODS:
            # 「读」作用域绝不能顺带放行写：凭证认证绕过了 MFA 与 sudo，
            # 一个 asset:read 的 key 能 POST 主机的话比漏配权限更糟
            raise ApiKeyScopeDenied(
                f'凭证「{credential.name}」的作用域为只读，不允许 {request.method}。')
        missing = [scope for scope in self.scopes if not credential.has_scope(scope)]
        if missing:
            logger.warning('凭证 %s 缺少作用域 %s', credential.key_id, missing)
            raise ApiKeyScopeDenied(f'凭证「{credential.name}」缺少作用域：{"、".join(missing)}。')
        return True


def scope_required(*scopes, read_only=False):
    """生成要求指定作用域的权限类（用法见模块开头）。

    read_only=True 时只放行安全方法——挂在既有的读写 ViewSet 上时必须带它，
    否则一个「读」作用域的 key 顺带拿到了写权限。

    作用域拼错在这里就报错：写成权限类属性的话，要到线上调用返回 403 才发现。
    """
    unknown = [scope for scope in scopes if scope not in SCOPES]
    if unknown:
        raise ImproperlyConfigured(f'未登记的作用域：{unknown}，请先在 apikey.SCOPES 中登记。')
    name = 'HasApiScope' + ''.join(
        part.title() for scope in scopes for part in scope.replace(':', '_').split('_'))
    if read_only:
        name += 'ReadOnly'
    return type(name, (HasApiScope,), {'scopes': tuple(scopes), 'read_only': read_only})


def request_credential(request):
    """本次请求的 API 凭证；不是凭证认证进来的返回 None。

    给视图收窄 queryset 用（见 ApiCredential.narrow），避免各处重复 isinstance。
    """
    credential = getattr(request, 'auth', None)
    return credential if isinstance(credential, ApiCredential) else None


class UserOrScope(BasePermission):
    """按认证方式分流：凭证认证判作用域，JWT/匿名交给原有权限类。

    **为什么不能写成 `module_perm('x') | scope_required(...)`**：凭证绑定的是
    真实服务账号，`request.user.is_authenticated` 为真，OR 的左边（只读放行）会
    直接命中——等于任何凭证不带任何作用域也能读。或语义在这里是个安全漏洞，
    必须按认证方式分流。

    用 user_or_scope() 生成子类，不直接用。
    """

    user_permission = None
    scopes = ()
    read_only = True

    def has_permission(self, request, view):
        if request_credential(request) is not None:
            return self._scope_permission().has_permission(request, view)
        return self.user_permission().has_permission(request, view)

    def has_object_permission(self, request, view, obj):
        if request_credential(request) is not None:
            return True  # 对象级由 queryset 收窄兜住（见 ApiCredential.narrow）
        checker = self.user_permission()
        if hasattr(checker, 'has_object_permission'):
            return checker.has_object_permission(request, view, obj)
        return True

    @classmethod
    def _scope_permission(cls):
        return scope_required(*cls.scopes, read_only=cls.read_only)()


def user_or_scope(user_permission, *scopes, read_only=True):
    """生成「登录用户走 user_permission、机读凭证走作用域」的权限类。

        permission_classes = [
            user_or_scope(module_perm('cmdb'), 'asset:read'),
            sudo_required_for('PUT', 'PATCH', 'DELETE'),
        ]
    """
    unknown = [scope for scope in scopes if scope not in SCOPES]
    if unknown:
        raise ImproperlyConfigured(f'未登记的作用域：{unknown}，请先在 apikey.SCOPES 中登记。')
    name = f'{user_permission.__name__}Or' + ''.join(
        part.title() for scope in scopes for part in scope.replace(':', '_').split('_'))
    return type(name, (UserOrScope,), {
        'user_permission': user_permission,
        'scopes': tuple(scopes),
        'read_only': read_only,
    })


# ── 限速 ──────────────────────────────────────────────────
class ApiKeyThrottle(SimpleRateThrottle):
    """按 key_id 分桶限速；凭证上填了 rate_limit 就用它，否则用全局默认。

    这是 key 泄露后唯一还在拦的东西：白名单能挡住来源，但泄露方本身就在
    白名单里时（比如 CI 机被人登上去），只有限速能限制损害速率。
    """

    scope = 'apikey'

    def allow_request(self, request, view):
        credential = getattr(request, 'auth', None)
        if isinstance(credential, ApiCredential) and credential.rate_limit:
            self.rate = credential.rate_limit
            self.num_requests, self.duration = self.parse_rate(self.rate)
        return super().allow_request(request, view)

    def get_cache_key(self, request, view):
        credential = getattr(request, 'auth', None)
        if not isinstance(credential, ApiCredential):
            return None  # 非凭证认证（登录用户）不参与该桶
        return f'throttle_{self.scope}_{credential.key_id}'


# ── 服务账号 ──────────────────────────────────────────────
def ensure_service_user(name):
    """取或建凭证绑定的服务账号。

    账号名 `svc-<凭证名>`，密码置为不可用（`set_unusable_password`）——凭证
    走的是 key 校验，这个账号只用来给审计记录一个可读的调用方身份，不该能
    从登录页进来。非 staff：机读接口不该能碰只给管理员的写操作。

    复用同名账号时把 `is_service` 补上（该字段晚于服务账号引入），并重新启用
    ——之前那把凭证删除时它被停用过（见 retire_service_user）。
    """
    User = get_user_model()
    username = f'svc-{name}'[:150]
    user = User.objects.filter(username=username).first()
    if user:
        if not (user.is_service and user.is_active):
            user.is_service = True
            user.is_active = True
            user.save(update_fields=['is_service', 'is_active'])
        return user
    user = User(username=username, is_staff=False, is_active=True,
                is_service=True, department=SERVICE_DEPARTMENT)
    user.set_unusable_password()
    user.save()
    return user


def retire_service_user(user):
    """凭证删除后停用其服务账号（没有别的凭证还在用它时）。

    **停用而不是删除**：审计表的 user 外键是 SET_NULL，删掉账号会把历史机读调用的
    归属抹成空——「这次上报是哪把凭证干的」就再也查不到了，那比留一个停用账号糟得多。

    停用后它既登不进来（密码本就不可用）也认不过凭证校验（认证类会检查
    `is_active`），且默认不出现在用户列表里（UserViewSet 过滤 is_service）。
    """
    if not (user and getattr(user, 'is_service', False)):
        return False
    if user.api_credentials.exists():
        return False  # 还有别的凭证绑着它
    if not user.is_active:
        return False
    user.is_active = False
    user.save(update_fields=['is_active'])
    logger.info('凭证删除后停用服务账号：%s（审计历史保留）', user.username)
    return True
