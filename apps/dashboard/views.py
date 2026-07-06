from datetime import timedelta

from django.db.models import Count
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.asset_mgr.models import Server
from apps.audit.models import AuditLog
from common.response import ok


class DashboardStatsView(APIView):
    """概览统计：服务器、用户、审计日志聚合数据。"""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        now = timezone.now()
        today = now.date()

        server_status = dict(
            Server.objects.values_list('status').annotate(c=Count('id'))
        )

        # 近 7 天每日操作数（按日期升序）
        seven_days_ago = today - timedelta(days=6)
        daily_raw = (
            AuditLog.objects.filter(created_at__date__gte=seven_days_ago)
            .values_list('created_at__date')
            .annotate(c=Count('id'))
        )
        daily_map = {str(d): c for d, c in daily_raw}
        recent_actions = [
            {'date': str(seven_days_ago + timedelta(days=i)),
             'count': daily_map.get(str(seven_days_ago + timedelta(days=i)), 0)}
            for i in range(7)
        ]

        return ok({
            'servers': {
                'total': Server.objects.count(),
                'running': server_status.get('running', 0),
                'stopped': server_status.get('stopped', 0),
                'maintenance': server_status.get('maintenance', 0),
            },
            'users': {
                'total': User.objects.count(),
                'active': User.objects.filter(is_active=True).count(),
            },
            'audit': {
                'total': AuditLog.objects.count(),
                'today': AuditLog.objects.filter(created_at__date=today).count(),
            },
            'recent_actions': recent_actions,
        })
