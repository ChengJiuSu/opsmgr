"""把超时未审批的 DNS 变更单标为已过期。

不标也不会被执行（审批时会检查过期并就地标记），这个命令只是让列表与统计口径
干净：一堆看着"待审批"实则批不动的单子会让人不再相信这个列表。

建议挂 cron 每小时执行：

    python manage.py expire_dns_requests
"""
from django.core.management.base import BaseCommand

from apps.cloud_mgr import dns_approval


class Command(BaseCommand):
    help = '把超过有效期的待审批 DNS 变更单标为已过期'

    def handle(self, *args, **options):
        count = dns_approval.expire_stale()
        self.stdout.write(self.style.SUCCESS(f'已标记 {count} 张过期 DNS 变更单'))
