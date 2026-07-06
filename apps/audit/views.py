from rest_framework import mixins, viewsets
from rest_framework.permissions import IsAuthenticated

from .models import AuditLog
from .serializers import AuditLogSerializer


class AuditLogViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    queryset = AuditLog.objects.select_related('user').all()
    serializer_class = AuditLogSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params

        username = params.get('username')
        method = params.get('method')
        status_code = params.get('status_code')
        keyword = params.get('keyword')
        date_from = params.get('date_from')
        date_to = params.get('date_to')

        if username:
            qs = qs.filter(user__username__icontains=username)
        if method:
            qs = qs.filter(method=method.upper())
        if status_code and status_code.isdigit():
            qs = qs.filter(status_code=int(status_code))
        if keyword:
            qs = qs.filter(path__icontains=keyword)
        if date_from:
            qs = qs.filter(created_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(created_at__date__lte=date_to)
        return qs
