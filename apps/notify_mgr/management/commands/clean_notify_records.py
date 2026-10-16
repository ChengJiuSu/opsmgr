"""清理过期的通知事件与发送记录（celery beat 每天跑一次，也可手动执行）。"""
from django.conf import settings
from django.core.management.base import BaseCommand

from apps.notify_mgr.tasks import clean_records


class Command(BaseCommand):
    help = '清理超过保留期的通知事件与发送记录'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=None,
                            help=f'保留天数（默认 NOTIFY_RECORD_RETENTION_DAYS='
                                 f'{settings.NOTIFY_RECORD_RETENTION_DAYS}）')

    def handle(self, *args, **options):
        count = clean_records(options['days'])
        self.stdout.write(self.style.SUCCESS(f'已清理 {count} 条通知事件'))
