from rest_framework import viewsets

from apps.auth_mgr.permissions import module_perm
from common.sudo import SudoRequired

from .models import SystemConfig
from .serializers import SystemConfigSerializer


class SystemConfigViewSet(viewsets.ModelViewSet):
    queryset = SystemConfig.objects.all().order_by('key')
    serializer_class = SystemConfigSerializer
    # 需要 system 模块 write 档：这张表里放着 jenkins.api_token、mfa.enforce_scope
    # 等全站安全策略，改它等于改安全边界。**原来是 IsAuthenticated**——任何
    # 登录用户过了 sudo 就能改，属漏配，随角色矩阵一并收紧。
    # min_level='write' 而不是按方法推导：这些配置值本身就敏感，读也不该对所有人开放。
    # 另外要求 sudo 二次验证：改了就能拿到更多东西，而登录那次 MFA 可能是 60 分钟前的事
    permission_classes = [module_perm('system', min_level='write'), SudoRequired]
