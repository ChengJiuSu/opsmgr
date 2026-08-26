from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from common.sudo import SudoRequired

from .models import SystemConfig
from .serializers import SystemConfigSerializer


class SystemConfigViewSet(viewsets.ModelViewSet):
    queryset = SystemConfig.objects.all().order_by('key')
    serializer_class = SystemConfigSerializer
    # 配置面写操作要求 sudo 二次验证（common/sudo.py）：改了就能拿到更多东西，
    # 而登录那次 MFA 可能是 60 分钟前的事
    # 这张表里放着 jenkins.api_token、artifact.api_key、mfa.enforce_scope，
    # 改它等于改全站安全策略
    permission_classes = [IsAuthenticated, SudoRequired]
