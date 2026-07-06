from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db.models import Q
from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView

from common.permissions import IsAdmin
from common.response import fail, ok

from .serializers import (
    ChangePasswordSerializer,
    CustomTokenObtainPairSerializer,
    UserSerializer,
)

User = get_user_model()


class LoginView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer


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
