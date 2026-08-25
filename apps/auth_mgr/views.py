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
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView

from common.permissions import IsAdmin
from common.ratelimit import blocked_seconds, record_failure, reset
from common.request import client_ip
from common.response import fail, ok

from .serializers import (
    ChangePasswordSerializer,
    CustomTokenObtainPairSerializer,
    UserSerializer,
)

User = get_user_model()
logger = logging.getLogger(__name__)

# 限速分桶：按用户名与按客户端 IP 各算一份（阈值见 settings）
SCOPE_USER = 'login_user'
SCOPE_IP = 'login_ip'


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

        try:
            response = super().post(request, *args, **kwargs)
        except APIException:
            # 凭据错误 / 账号停用：simplejwt 抛 AuthenticationFailed 或校验异常
            self._record_failure(buckets, ip, username)
            raise

        if response.status_code == 200:
            for scope, ident in buckets:
                if ident:
                    reset(scope, ident)
        else:
            self._record_failure(buckets, ip, username)
        return response

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
        return ok(msg='密码修改成功')


class UserViewSet(viewsets.ModelViewSet):
    """用户管理（仅管理员）。"""

    queryset = User.objects.all().order_by('-date_joined')
    serializer_class = UserSerializer
    permission_classes = [IsAdmin]

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
