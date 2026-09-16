"""飞书配置面接口（管理员维护，回调入口不在这里）。

权限统一挂 `module_perm('system', min_level='write')` + `SudoRequired`：

- **不新开权限模块**：飞书应用配置与 LDAP / SMTP 同一个风险面——含连接口令、
  且直接影响登录信任链（改 app_id 指向别的飞书应用 + 打开自动建号 = 任意人可
  登录），按 roles.py 的拆分判据本就归 `system`；
- **min_level='write' 而不是按方法推导**：读到的就是 App ID、回调地址与 IP
  白名单，等于一份「怎么打这两条公网入口」的说明书，GET 也不能对所有人开；
- **sudo**：改这里等于改安全边界，而登录那次 MFA 可能是 60 分钟前的事。
"""
import hmac
import logging

from django.conf import settings
from django.contrib.auth.models import update_last_login
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from apps.auth_mgr.permissions import module_perm
from common.pagination import StandardPagination
from common.ratelimit import blocked_seconds, record_failure, reset
from common.request import auth_client_ip, client_ip
from common.response import fail, ok
from common.sudo import SudoRequired

from . import client, crypto, dedupe, handlers, sso
from .client import FeishuError
from .models import FeishuApp, FeishuBinding, FeishuChannel
from .serializers import (
    FeishuAppSerializer,
    FeishuBindingSerializer,
    FeishuChannelSerializer,
)

logger = logging.getLogger(__name__)

#: 飞书登录的失败限速分桶（按来源 IP；走到这一步还不知道是谁）
SSO_SCOPE_IP = 'feishu_sso_ip'

CONFIG_PERMISSIONS = [module_perm('system', min_level='write'), SudoRequired]


class FeishuAppView(APIView):
    """GET / PUT /api/feishu/app/ —— 飞书应用配置（单例）。

    单例用 APIView 而不是 ViewSet：列表/详情/新增/删除这几个动作在这里都没有
    语义，暴露出来只会让调用方需要先知道「id 恒为 1」这种内部约定。
    """

    permission_classes = CONFIG_PERMISSIONS

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request):
        app = FeishuApp.load()
        return ok(FeishuAppSerializer(app, context={'request': request}).data)

    @extend_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT)
    def put(self, request):
        app = FeishuApp.load()
        serializer = FeishuAppSerializer(
            app, data=request.data, partial=True, context={'request': request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return ok(serializer.data, msg='已保存。')


class FeishuAppCallbackSlugView(APIView):
    """POST /api/feishu/app/rotate-slug/ —— 轮换回调标识（换回调地址）。

    独立动作而不是让 PUT 改 `callback_slug`：换了之后飞书后台的事件订阅与卡片
    回调地址会立刻失效，必须是一次有意识的操作，不能夹在一次普通保存里发生。
    """

    permission_classes = CONFIG_PERMISSIONS

    @extend_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT)
    def post(self, request):
        app = FeishuApp.load()
        app.rotate_callback_slug()
        data = FeishuAppSerializer(app, context={'request': request}).data
        return ok(data, msg='回调地址已轮换，请到飞书开放平台同步更新事件订阅与卡片回调地址。')


class FeishuChannelViewSet(viewsets.ModelViewSet):
    """飞书会话白名单（群 / 单聊）。"""

    queryset = FeishuChannel.objects.all()
    serializer_class = FeishuChannelSerializer
    permission_classes = CONFIG_PERMISSIONS
    pagination_class = StandardPagination

    def get_queryset(self):
        qs = super().get_queryset()
        chat_type = self.request.query_params.get('chat_type')
        if chat_type:
            qs = qs.filter(chat_type=chat_type)
        enabled = self.request.query_params.get('enabled')
        if enabled in ('true', 'false'):
            qs = qs.filter(enabled=enabled == 'true')
        allow_execute = self.request.query_params.get('allow_execute')
        if allow_execute in ('true', 'false'):
            qs = qs.filter(allow_execute=allow_execute == 'true')
        keyword = self.request.query_params.get('keyword')
        if keyword:
            from django.db.models import Q

            qs = qs.filter(Q(name__icontains=keyword) | Q(chat_id__icontains=keyword))
        return qs


# ══════════════════════════════════════════════════════════
# 回调入口（公网，免登录）
# ══════════════════════════════════════════════════════════

class FeishuCallbackThrottle(SimpleRateThrottle):
    """按来源 IP 给回调入口限速。

    验签发生在应用层，扫描流量在到达验签之前就该被挡掉，否则每个探测包都会
    让后端做一次 SHA256 + 一次 AES。nginx 的 limit_req 是同一目的的外层，
    两层都要——nginx 那层管不到「已经在 IP 白名单里、但开始刷请求」的情况。
    """

    scope = 'feishu_callback'

    def get_cache_key(self, request, view):
        # 鉴权口径的 IP（可信代理链解析），不吃伪造的 XFF
        return f'throttle_{self.scope}_{auth_client_ip(request) or "unknown"}'


class FeishuCallbackView(APIView):
    """飞书回调入口基类：只管「这条请求可不可信」与「怎么回包」。

    **认证与权限刻意置空**：这条路走的是签名校验，不是 JWT。`authentication_classes
    = []` 覆盖全局的 MfaJWTAuthentication（否则请求会被 MFA 强制挡在门外），
    与机器接口 X-API-KEY 走自己的认证类是同一类处置。

    **回包恒定**：无论哪一道没过，一律 200 + 固定包体，失败原因只进日志。回包
    有差异本身就是一份探测反馈——攻击者能据此区分「路径不对」「IP 不对」
    「签名不对」，逐步逼近。飞书后台的地址验证失败时，看服务端日志，不看回包。

    门的顺序（贵的排后面）：
    路径随机串 → 配置就绪 → 来源 IP → 签名/时间戳/nonce/解密/token → 幂等。
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [FeishuCallbackThrottle]

    #: 恒定回包体
    ACK = {'code': 0}
    #: 幂等窗口：飞书的投递重试跨度按小时算
    DEDUPE_TTL = 3600
    #: 子类覆盖：dedupe 的命名空间
    DEDUPE_NAMESPACE = 'callback'

    def _ack(self):
        return Response(self.ACK)

    def _reject_reason(self, request, app, slug):
        """返回中文拒绝原因；None 表示可以继续。"""
        if not hmac.compare_digest(str(slug), str(app.callback_slug)):
            return '回调标识不匹配'
        if not app.is_ready():
            missing = app.missing_fields()
            return f'飞书接入未就绪（{"、".join(missing) if missing else "未启用"}）'
        if not app.ip_allowed(auth_client_ip(request)):
            return '来源 IP 不在白名单内'
        return None

    def post(self, request, slug):
        app = FeishuApp.load()
        ip = auth_client_ip(request)

        reason = self._reject_reason(request, app, slug)
        if reason:
            logger.warning('飞书回调被拒：%s（path=%s ip=%s）', reason, request.path, ip)
            return self._ack()

        try:
            payload = crypto.verify_callback(
                app,
                request.headers.get('X-Lark-Request-Timestamp'),
                request.headers.get('X-Lark-Request-Nonce'),
                request.headers.get('X-Lark-Signature'),
                # 必须是原始字节：解析后重新序列化会改键序与空格，签名必不匹配
                request.body,
            )
        except crypto.CallbackError as exc:
            logger.warning('飞书回调校验失败：%s（path=%s ip=%s）', exc, request.path, ip)
            return self._ack()

        # 事件订阅地址验证：飞书要求原样回显 challenge，这一条不能走恒定回包
        if payload.get('type') == 'url_verification':
            logger.info('飞书回调地址验证通过（path=%s）', request.path)
            return Response({'challenge': str(payload.get('challenge') or '')})

        if not self._first_delivery(payload):
            return self._ack()

        try:
            extra = self.dispatch_payload(app, payload)
        except Exception:  # noqa: BLE001 - 业务异常不能改变回包形态
            logger.exception('飞书回调处理异常（path=%s）', request.path)
            return self._ack()
        return Response(extra if isinstance(extra, dict) else self.ACK)

    def _first_delivery(self, payload):
        """事件/动作级幂等：飞书的投递重试换了 nonce 与签名，nonce 判重挡不住。"""
        event_id = handlers.event_id(payload)
        if not event_id:
            return True  # 没有 event_id 的形态，靠 nonce 判重兜底
        try:
            fresh = dedupe.consume(self.DEDUPE_NAMESPACE, event_id, self.DEDUPE_TTL)
        except dedupe.DedupeUnavailable:
            logger.warning('判重缓存不可用，丢弃回调 event_id=%s（fail-closed）', event_id)
            return False
        if not fresh:
            logger.info('飞书回调重复投递，已忽略：event_id=%s', event_id)
        return fresh

    def dispatch_payload(self, app, payload):
        raise NotImplementedError


class FeishuEventView(FeishuCallbackView):
    """POST /api/feishu/callback/<slug>/event/ —— 事件订阅。"""

    DEDUPE_NAMESPACE = 'event'

    @extend_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT)
    def post(self, request, slug):
        return super().post(request, slug)

    def dispatch_payload(self, app, payload):
        return handlers.handle_event(app, payload)


class FeishuCardView(FeishuCallbackView):
    """POST /api/feishu/callback/<slug>/card/ —— 卡片按钮回调。

    回包用空对象而不是 `{"code": 0}`：卡片回调的响应体是留给 toast / 整张新
    卡片的，回一个无关的业务码没有意义。
    """

    DEDUPE_NAMESPACE = 'card'
    ACK = {}

    @extend_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT)
    def post(self, request, slug):
        return super().post(request, slug)

    def dispatch_payload(self, app, payload):
        return handlers.handle_card_action(app, payload)


# ══════════════════════════════════════════════════════════
# 飞书登录（SSO，免登录入口）
# ══════════════════════════════════════════════════════════

class FeishuSsoStatusView(APIView):
    """GET /api/feishu/sso/status/ —— 登录页据此决定要不要渲染「飞书登录」。

    公开接口，只回一个布尔。暴露「本站是否开了飞书登录」不构成信息泄露，
    而把按钮渲染出来再报错是更差的体验。
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request):
        app = FeishuApp.load()
        return ok({'enabled': bool(app.is_ready() and app.sso_enabled)})


class FeishuSsoAuthorizeView(APIView):
    """GET /api/feishu/sso/authorize/ —— 取授权跳转地址（含一次性 state）。"""

    authentication_classes = []
    permission_classes = [AllowAny]

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request):
        try:
            app = sso.get_app()
            url, state = sso.authorize_url(app, request)
        except sso.SsoError as exc:
            return fail(str(exc))
        return ok({'authorize_url': url, 'state': state})


class FeishuSsoCallbackView(APIView):
    """POST /api/feishu/sso/callback/ —— 授权码换平台令牌。

    与密码登录同样按 IP 限速：这条路能拿到正式令牌，不限速就是给撞库留门。
    按 IP 而不是按用户：走到这一步还不知道是谁。
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    @extend_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT)
    def post(self, request):
        ip = client_ip(request)
        remaining = blocked_seconds(SSO_SCOPE_IP, ip) if ip else 0
        if remaining:
            minutes = max(1, -(-remaining // 60))
            return fail(f'登录失败次数过多，请 {minutes} 分钟后再试。',
                        code=status.HTTP_429_TOO_MANY_REQUESTS,
                        status=status.HTTP_429_TOO_MANY_REQUESTS)

        try:
            app = sso.get_app()
            user, tokens = sso.login(
                app, request.data.get('code'), request.data.get('state'), request)
        except (sso.SsoError, FeishuError) as exc:
            if ip:
                record_failure(SSO_SCOPE_IP, ip, limit=settings.LOGIN_FAIL_IP_LIMIT,
                               window=settings.LOGIN_FAIL_WINDOW,
                               block=settings.LOGIN_FAIL_BLOCK)
            logger.warning('飞书登录失败（ip=%s）：%s', ip, exc)
            return fail(str(exc), status=status.HTTP_401_UNAUTHORIZED)

        if ip:
            reset(SSO_SCOPE_IP, ip)
        update_last_login(None, user)
        logger.info('用户 %s 经飞书登录成功（ip=%s）', user.username, ip)
        return Response(tokens)


class FeishuMyBindingView(APIView):
    """GET /api/feishu/sso/binding/ —— 当前用户的飞书绑定状态（个人页展示）。

    只读。**不提供自助解绑**：没有可用密码的账号（自动创建的）解绑后就再也进不
    来了，解绑属于管理动作，放在用户管理里由管理员操作。

    union_id 只回显尾段：它是内部标识，展示只为让人确认「绑的是我这个飞书号」，
    给全量没有用处。
    """

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request):
        app = FeishuApp.load()
        binding = FeishuBinding.objects.filter(user=request.user).first()
        data = {
            'sso_enabled': bool(app.is_ready() and app.sso_enabled),
            'bound': binding is not None,
        }
        if binding:
            data.update({
                'feishu_name': binding.feishu_name,
                'feishu_email': binding.feishu_email,
                'union_id_tail': binding.union_id[-6:],
                'source': binding.source,
                'source_display': binding.get_source_display(),
                'last_login_at': binding.last_login_at,
                'created_at': binding.created_at,
            })
        return ok(data)


class FeishuBindingViewSet(mixins.ListModelMixin,
                           mixins.CreateModelMixin,
                           mixins.DestroyModelMixin,
                           viewsets.GenericViewSet):
    """飞书绑定关系管理（管理员）。

    权限挂 `account` 而不是 `system`：绑定关系决定「哪个飞书号能以哪个平台账号
    登录与发起执行」，和用户、角色、API 凭证是同一个身份面——改这里等于改身份
    映射，属 account 的风险面。`account` 只有 none/write 两档，读也需要 write：
    绑定列表本身就是一份「谁能从飞书进来」的清单。

    **没有更新接口**：改绑 = 解绑再绑，两步各自留痕比一次静默改值清楚得多。
    """

    queryset = FeishuBinding.objects.select_related('user').all()
    serializer_class = FeishuBindingSerializer
    permission_classes = [module_perm('account', min_level='write'), SudoRequired]
    pagination_class = StandardPagination

    def get_queryset(self):
        qs = super().get_queryset()
        keyword = self.request.query_params.get('keyword')
        if keyword:
            from django.db.models import Q

            qs = qs.filter(Q(user__username__icontains=keyword)
                           | Q(feishu_name__icontains=keyword)
                           | Q(feishu_email__icontains=keyword)
                           | Q(union_id__icontains=keyword))
        return qs

    def perform_create(self, serializer):
        binding = serializer.save(source='manual')
        logger.warning('管理员 %s 手工绑定飞书账号 %s → 平台用户 %s',
                       self.request.user.username, binding.union_id,
                       binding.user.username)

    def destroy(self, request, *args, **kwargs):
        binding = self.get_object()
        username = binding.user.username
        binding.delete()
        logger.warning('管理员 %s 解除了平台用户 %s 的飞书绑定',
                       request.user.username, username)
        return ok(msg=f'已解除「{username}」的飞书绑定。')

    @extend_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT)
    @action(detail=False, methods=['post'])
    def lookup(self, request):
        """按邮箱 / 手机号在飞书通讯录里反查用户标识。

        手工绑定时管理员手上只有邮箱或手机号，让他去别处翻 union_id 不现实。
        查不到就明确说「通讯录里没有」，不返回半个 id——拿半个 id 建出来的绑定
        永远匹配不上回调里的身份。
        """
        email = str(request.data.get('email') or '').strip()
        mobile = str(request.data.get('mobile') or '').strip()
        if not (email or mobile):
            return fail('请提供邮箱或手机号')
        try:
            ids = client.lookup_user_ids(email=email or None, mobile=mobile or None)
        except FeishuError as exc:
            return fail(str(exc))
        if not ids:
            return fail('飞书通讯录中未找到该邮箱/手机号对应的成员。')

        name = ''
        if ids.get('open_id'):
            try:
                name = str(client.get_user(ids['open_id']).get('name') or '')
            except FeishuError:
                # 名字只是给人看的，查不到不影响绑定本身
                logger.info('飞书用户名查询失败，绑定照常进行')
        return ok({'union_id': ids.get('union_id', ''),
                   'open_id': ids.get('open_id', ''),
                   'name': name, 'email': email, 'mobile': mobile})
