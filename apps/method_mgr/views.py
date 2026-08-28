from rest_framework import viewsets

from apps.auth_mgr.permissions import module_perm

from .models import Method
from .serializers import MethodSerializer


class MethodViewSet(viewsets.ModelViewSet):
    """平台方法管理：管理员维护，已认证用户可查看（用于执行时选择）。"""

    queryset = Method.objects.select_related('service_type')
    serializer_class = MethodSerializer
    permission_classes = [module_perm('method')]

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        provider = params.get('provider')
        enabled = params.get('enabled')
        keyword = params.get('keyword')
        if provider:
            qs = qs.filter(provider=provider)
        if enabled in ('true', 'false'):
            qs = qs.filter(enabled=(enabled == 'true'))
        if keyword:
            qs = qs.filter(name__icontains=keyword)
        return qs
