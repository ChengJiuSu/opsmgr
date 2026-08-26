import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db.models import Q
from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView

from rest_framework_simplejwt.exceptions import TokenError as SimpleJWTTokenError

from common import sudo
from common.permissions import IsAdmin
from common.ratelimit import blocked_seconds, record_failure, reset
from common.request import client_ip
from common.response import fail, ok

from . import services
from .serializers import (
    ChangePasswordSerializer,
    CustomTokenObtainPairSerializer,
    MfaCodeSerializer,
    MfaSensitiveSerializer,
    MfaVerifySerializer,
    SudoSerializer,
    UserSerializer,
)
from .tokens import (
    consume_challenge,
    count_attempt,
    issue_after_mfa,
    issue_challenge,
    load_challenge,
)

User = get_user_model()
logger = logging.getLogger(__name__)

# 限速分桶：按用户名与按客户端 IP 各算一份（阈值见 settings）
SCOPE_USER = 'login_user'
SCOPE_IP = 'login_ip'
SCOPE_MFA_USER = 'mfa_user'
SCOPE_SUDO_USER = 'sudo_user'


class LoginView(TokenObtainPairView):
    """JWT 登录，带失败限速。

    窗口内失败达阈值即锁定，锁定期直接 429 且**不再校验密码**——密码正确也
    不放行，否则限速只是给攻击者多一次尝试机会。用户名与 IP 分开计数，IP
    阈值宽松（办公网共用出口 IP，见 settings.LOGIN_FAIL_IP_LIMIT 注释）。
    缓存不可用时放行并告警（common.ratelimit 的 fail-open 取舍）。
    """

    serializer_class = CustomTokenObtainPairSerializer

    def post(self, request, *args, **kwargs):
        username = str(request.data.get('username') or '')[:150].lower()
        ip = client_ip(request)
        buckets = [(SCOPE_USER, username), (SCOPE_IP, ip)]

        for scope, ident in buckets:
            if not ident:
                continue
            remaining = blocked_seconds(scope, ident)
            if remaining:
                logger.warning('登录被限速：scope=%s ip=%s user=%s 剩余 %ss',
                               scope, ip, username, remaining)
                minutes = max(1, -(-remaining // 60))  # 向上取整，别显示「0 分钟后」
                return fail(f'登录失败次数过多，请 {minutes} 分钟后再试。',
                            code=status.HTTP_429_TOO_MANY_REQUESTS,
                            status=status.HTTP_429_TOO_MANY_REQUESTS)

        serializer = self.get_serializer(data=request.data)
        try:
            serializer.is_valid(raise_exception=True)
        except APIException:
            # 凭据错误 / 账号停用：simplejwt 抛 AuthenticationFailed 或校验异常
            self._record_failure(buckets, ip, username)
            raise

        user = serializer.user
        for scope, ident in buckets:
            if ident:
                reset(scope, ident)

        if services.is_enabled(user):
            # 已绑定：这一步不发访问令牌，只发短时效挑战票据（token_type='mfa'，
            # 当 Bearer 用会被认证类拒），换正式令牌见 /api/auth/mfa/verify/
            token, ttl = issue_challenge(user)
            logger.info('用户 %s 密码通过，等待 MFA 校验', user.username)
            return Response({'mfa_required': True, 'mfa_token': token, 'expires_in': ttl})

        data = dict(serializer.validated_data)
        if services.required_for(user):
            # 在强制范围内却没绑：正常发令牌（amr 只有 pwd），但前端要立刻跳
            # 绑定页；除白名单外的接口由 MfaJWTAuthentication 挡住
            data['mfa_enrollment_required'] = True
        return Response(data)

    @staticmethod
    def _record_failure(buckets, ip, username):
        limits = {SCOPE_USER: settings.LOGIN_FAIL_LIMIT,
                  SCOPE_IP: settings.LOGIN_FAIL_IP_LIMIT}
        for scope, ident in buckets:
            if not ident:
                continue
            blocked = record_failure(
                scope, ident, limit=limits[scope],
                window=settings.LOGIN_FAIL_WINDOW, block=settings.LOGIN_FAIL_BLOCK)
            if blocked:
                logger.warning('登录失败超阈值，锁定 %ss：scope=%s ip=%s user=%s',
                               blocked, scope, ip, username)


class MfaVerifyView(APIView):
    """登录第二步：挑战票据 + 验证码（或恢复码）→ 正式令牌。"""

    permission_classes = [AllowAny]
    authentication_classes = []  # 此时还没有访问令牌

    @extend_schema(request=MfaVerifySerializer, responses=OpenApiResponse(
        description='access + refresh'))
    def post(self, request):
        serializer = MfaVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ip = client_ip(request)

        try:
            challenge = load_challenge(serializer.validated_data['mfa_token'])
        except SimpleJWTTokenError as exc:
            return fail(f'{exc}'.strip() or '验证票据无效或已过期，请重新登录。',
                        status=status.HTTP_401_UNAUTHORIZED)

        user = User.objects.filter(pk=challenge.get('user_id')).first()
        if user is None or not user.is_active:
            consume_challenge(challenge)
            return fail('账号不存在或已停用。', status=status.HTTP_401_UNAUTHORIZED)

        # 票据内计次之外再按用户/IP 计一份：换票据重试同样受限
        for scope, ident in ((SCOPE_MFA_USER, user.username.lower()), (SCOPE_IP, ip)):
            if ident and blocked_seconds(scope, ident):
                return fail('验证失败次数过多，请稍后再试。',
                            code=status.HTTP_429_TOO_MANY_REQUESTS,
                            status=status.HTTP_429_TOO_MANY_REQUESTS)

        try:
            method = services.verify(user, serializer.validated_data['code'])
        except services.MfaError as exc:
            remaining = count_attempt(challenge)
            self._record_mfa_failure(user, ip)
            if remaining == 0:
                return fail('验证码错误次数过多，请重新登录。',
                            status=status.HTTP_401_UNAUTHORIZED)
            return fail(f'{exc}（还可尝试 {remaining} 次）',
                        status=status.HTTP_400_BAD_REQUEST)

        consume_challenge(challenge)
        reset(SCOPE_MFA_USER, user.username.lower())
        if ip:
            reset(SCOPE_IP, ip)
        logger.info('用户 %s 通过 MFA 校验（%s）', user.username, method)
        data = issue_after_mfa(user)
        if method == 'recovery':
            data['recovery_codes_left'] = services.status(user)['recovery_codes_left']
        return Response(data)

    @staticmethod
    def _record_mfa_failure(user, ip):
        limits = ((SCOPE_MFA_USER, user.username.lower(), settings.LOGIN_FAIL_LIMIT),
                  (SCOPE_IP, ip, settings.LOGIN_FAIL_IP_LIMIT))
        for scope, ident, limit in limits:
            if ident:
                record_failure(scope, ident, limit=limit,
                               window=settings.LOGIN_FAIL_WINDOW,
                               block=settings.LOGIN_FAIL_BLOCK)


class SudoView(APIView):
    """敏感操作二次验证窗口：GET 查剩余时间，POST 开窗口。

    哪些接口需要它见 common/sudo.py 的 SudoRequired 使用点（凭据、用户、
    Agent 与通道路由、云账号、系统配置——都是「改了就能拿到更多东西」的配置面，
    不含任务流执行，日常发布不受影响）。
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiResponse(description='active / expires_in'))
    def get(self, request):
        jti = sudo.token_jti(request)
        active = sudo.is_active(request.user.pk, jti)
        return ok({'active': active, 'ttl': settings.SUDO_TTL,
                   'mfa_enabled': services.is_enabled(request.user)})

    @extend_schema(request=SudoSerializer,
                   responses=OpenApiResponse(description='expires_in'))
    def post(self, request):
        serializer = SudoSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = request.user
        ident = user.username.lower()

        jti = sudo.token_jti(request)
        if sudo.is_active(user.pk, jti):
            # 幂等：窗口已开着就直接成功。前端可能重复提交（填满自动提交 + 回车），
            # 第二次拿同一个码去校验必然撞防重放，报错却已放行，最费解
            return ok({'expires_in': settings.SUDO_TTL}, msg='二次验证已生效')

        remaining = blocked_seconds(SCOPE_SUDO_USER, ident)
        if remaining:
            return fail('验证失败次数过多，请稍后再试。',
                        code=status.HTTP_429_TOO_MANY_REQUESTS,
                        status=status.HTTP_429_TOO_MANY_REQUESTS)

        error = self._check(user, serializer.validated_data)
        if error:
            record_failure(SCOPE_SUDO_USER, ident, limit=settings.LOGIN_FAIL_LIMIT,
                           window=settings.LOGIN_FAIL_WINDOW,
                           block=settings.LOGIN_FAIL_BLOCK)
            return fail(error)

        if not jti:
            # 非 JWT 认证（机器接口）不该走到这里；给明确原因而不是静默失败
            return fail('当前认证方式不支持二次验证。')
        try:
            ttl = sudo.grant(user.pk, jti)
        except Exception:  # noqa: BLE001 - 缓存不可用时明确拒绝（fail-closed）
            logger.exception('sudo 窗口写入失败：user=%s', user.username)
            return fail('二次验证服务暂不可用（缓存故障），请稍后再试。',
                        status=status.HTTP_503_SERVICE_UNAVAILABLE)
        reset(SCOPE_SUDO_USER, ident)
        logger.info('用户 %s 开启 sudo 窗口（%ss）', user.username, ttl)
        return ok({'expires_in': ttl}, msg='二次验证通过')

    @staticmethod
    def _check(user, data):
        """校验二次验证凭证，通过返回 None，否则返回中文原因。"""
        if services.is_enabled(user):
            # 启用了 MFA 就必须用验证器/恢复码：密码可能被钓走，验证器不会
            if not data.get('code'):
                return '请输入验证器上的 6 位验证码。'
            try:
                services.verify(user, data['code'])
            except services.MfaError as exc:
                return str(exc)
            return None
        # 未启用 MFA 的账号退回密码确认，否则这道门在 enforce_scope=off 时形同虚设
        if not data.get('password'):
            return '请输入登录密码。'
        if not user.check_password(data['password']):
            return '密码不正确'
        return None


class MfaStatusView(APIView):
    """当前用户的 MFA 状态（前端据此显示绑定入口或已绑定）。"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return ok(services.status(request.user))


class MfaSetupView(APIView):
    """开始绑定：返回二维码 URI、手动密钥与恢复码（**仅此一次**明文可见）。"""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiResponse(description='secret / uri / recovery_codes'))
    def post(self, request):
        try:
            return ok(services.start_enrollment(request.user))
        except services.MfaError as exc:
            return fail(str(exc))


class MfaActivateView(APIView):
    """用一个正确验证码启用绑定（验过才启用，避免把自己锁在门外）。"""

    permission_classes = [IsAuthenticated]

    @extend_schema(request=MfaCodeSerializer, responses=OpenApiResponse(description='已启用'))
    def post(self, request):
        serializer = MfaCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            services.activate(request.user, serializer.validated_data['code'])
        except services.MfaError as exc:
            return fail(str(exc))
        # 绑定完成后换一套带 otp 声明的令牌，否则强制策略下当前会话仍被挡
        return ok(issue_after_mfa(request.user), msg='MFA 已启用')


class MfaDisableView(APIView):
    """解绑：需密码 + 当前验证码。"""

    permission_classes = [IsAuthenticated]

    @extend_schema(request=MfaSensitiveSerializer,
                   responses=OpenApiResponse(description='已解绑'))
    def post(self, request):
        error = _check_password_and_code(request)
        if error:
            return error
        services.disable(request.user)
        sudo.revoke(request.user.pk, sudo.token_jti(request))
        return ok(msg='MFA 已解绑')


class MfaRecoveryCodesView(APIView):
    """重新生成恢复码（旧码全部作废）：需密码 + 当前验证码。"""

    permission_classes = [IsAuthenticated]

    @extend_schema(request=MfaSensitiveSerializer,
                   responses=OpenApiResponse(description='新恢复码'))
    def post(self, request):
        error = _check_password_and_code(request)
        if error:
            return error
        try:
            codes = services.regenerate_recovery_codes(request.user)
        except services.MfaError as exc:
            return fail(str(exc))
        return ok({'recovery_codes': codes}, msg='恢复码已重新生成，旧码已全部作废')


def _check_password_and_code(request):
    """敏感操作双确认，通过返回 None，否则返回失败响应。"""
    serializer = MfaSensitiveSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    if not request.user.check_password(serializer.validated_data['password']):
        return fail('密码不正确')
    try:
        services.verify(request.user, serializer.validated_data['code'])
    except services.MfaError as exc:
        return fail(str(exc))
    return None


class LogoutView(APIView):
    """登出：将 refresh token 加入黑名单。"""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=inline_serializer('Logout', {'refresh': serializers.CharField()}),
        responses=OpenApiResponse(description='已登出'),
    )
    def post(self, request):
        refresh = request.data.get('refresh')
        if not refresh:
            return fail('缺少 refresh token')
        try:
            RefreshToken(refresh).blacklist()
        except TokenError:
            return fail('无效或已过期的 token')
        return ok(msg='已登出')


class MeView(APIView):
    """当前登录用户信息 / 更新个人资料。"""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=UserSerializer)
    def get(self, request):
        return ok(UserSerializer(request.user).data)

    @extend_schema(request=UserSerializer, responses=UserSerializer)
    def patch(self, request):
        # 个人资料只允许修改有限字段
        allowed = {'email', 'phone', 'department'}
        data = {k: v for k, v in request.data.items() if k in allowed}
        serializer = UserSerializer(request.user, data=data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return ok(serializer.data)


class ChangePasswordView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=ChangePasswordSerializer,
        responses=OpenApiResponse(description='密码修改成功'),
    )
    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        # 身份要素变了，之前那次「证明是本人」不再算数
        sudo.revoke(request.user.pk, sudo.token_jti(request))
        return ok(msg='密码修改成功')


class UserViewSet(viewsets.ModelViewSet):
    """用户管理（仅管理员）。"""

    queryset = User.objects.all().order_by('-date_joined')
    serializer_class = UserSerializer
    # 配置面写操作要求 sudo 二次验证（common/sudo.py）：改了就能拿到更多东西，
    # 而登录那次 MFA 可能是 60 分钟前的事
    # 建号/改权限/重置他人密码或 MFA——权限扩大面里最直接的一条
    permission_classes = [IsAdmin, sudo.SudoRequired]

    def get_queryset(self):
        qs = super().get_queryset()
        keyword = self.request.query_params.get('keyword')
        is_active = self.request.query_params.get('is_active')
        department = self.request.query_params.get('department')
        if keyword:
            qs = qs.filter(
                Q(username__icontains=keyword)
                | Q(email__icontains=keyword)
                | Q(phone__icontains=keyword)
            )
        if is_active in ('true', 'false'):
            qs = qs.filter(is_active=(is_active == 'true'))
        if department:
            qs = qs.filter(department=department)
        return qs

    def destroy(self, request, *args, **kwargs):
        if self.get_object() == request.user:
            return fail('不能删除当前登录用户')
        return super().destroy(request, *args, **kwargs)

    @action(detail=True, methods=['post'])
    def toggle_active(self, request, pk=None):
        user = self.get_object()
        if user == request.user:
            return fail('不能停用当前登录用户')
        user.is_active = not user.is_active
        user.save(update_fields=['is_active'])
        return ok({'is_active': user.is_active})

    @action(detail=True, methods=['post'])
    def reset_mfa(self, request, pk=None):
        """清除该用户的 MFA 绑定（丢手机时的兜底），用户下次登录需重新绑定。

        不构成越权：管理员本来就能 reset_password，清 MFA 之后仍要密码才能
        登录。操作会进审计与日志。
        """
        user = self.get_object()
        if not services.is_enabled(user) and services.get_binding(user) is None:
            return fail(f'用户 {user.username} 未绑定 MFA')
        services.reset(user)
        logger.warning('管理员 %s 重置了用户 %s 的 MFA 绑定',
                       request.user.username, user.username)
        return ok(msg=f'已清除 {user.username} 的 MFA 绑定')

    @action(detail=True, methods=['post'])
    def reset_password(self, request, pk=None):
        user = self.get_object()
        new_password = request.data.get('new_password')
        if not new_password:
            return fail('缺少 new_password')
        try:
            validate_password(new_password, user)
        except ValidationError as e:
            return fail('；'.join(e.messages))
        user.set_password(new_password)
        user.save(update_fields=['password'])
        return ok(msg='密码已重置')
