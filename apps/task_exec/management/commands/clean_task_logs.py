"""清理过期任务执行日志。

按 settings.TASK_LOG_RETENTION_DAYS（可用 --days 覆盖）删除过期的
TaskExecutionLog 记录及对应落盘文件。建议挂 cron / celery beat 每日执行：

    python manage.py clean_task_logs
"""
import logging
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.task_exec.models import TaskExecutionLog

logger = logging.getLogger('apps.task_exec')


class Command(BaseCommand):
    help = '清理超过保留期限的任务执行日志记录及落盘文件'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days', type=int, default=None,
            help='保留天数，默认取 TASK_LOG_RETENTION_DAYS',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='仅统计将删除的数量，不实际删除',
        )

    def handle(self, *args, **options):
        days = options['days'] or settings.TASK_LOG_RETENTION_DAYS
        cutoff = timezone.now() - timedelta(days=days)
        qs = TaskExecutionLog.objects.filter(created_at__lt=cutoff)
        total = qs.count()

        if options['dry_run']:
            self.stdout.write(f'[dry-run] 将删除 {total} 条 {days} 天前的执行日志')
            return

        log_root = Path(settings.TASK_LOG_DIR)
        removed_files = 0
        # 先删文件再删记录，避免记录已删而文件残留后无从索引
        for rel_path in qs.exclude(log_file='').values_list('log_file', flat=True).iterator():
            path = log_root / rel_path
            try:
                path.unlink(missing_ok=True)
                removed_files += 1
            except OSError:
                logger.warning('日志文件删除失败：%s', path)

        deleted, _ = qs.delete()

        # 顺带清理空的任务日志子目录
        if log_root.is_dir():
            for sub in log_root.iterdir():
                if sub.is_dir() and not any(sub.iterdir()):
                    sub.rmdir()

        self.stdout.write(f'已删除 {deleted} 条执行日志（{days} 天前），清理落盘文件 {removed_files} 个')
