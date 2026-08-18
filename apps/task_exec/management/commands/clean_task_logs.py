"""清理过期任务执行日志。

两个保留期各自独立（文件体积大，通常比记录清得更早）：
  - 落盘日志文件：settings.TASK_LOG_FILE_RETENTION_DAYS（--file-days 覆盖），
    按文件 mtime 扫 TASK_LOG_DIR 清理，顺带清掉记录已删的孤儿文件，
    对应记录的 log_file 置空（详情页自动退回数据库预览）；
    执行现场归档目录 <log_id>.d/（inventory/变量/命令）同保留期一并清理；
  - TaskExecutionLog 记录：settings.TASK_LOG_RETENTION_DAYS（--days 覆盖）。

建议挂 cron / celery beat 每日执行：

    python manage.py clean_task_logs
"""
import logging
import shutil
import time
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.task_exec.models import TaskExecutionLog

logger = logging.getLogger('apps.task_exec')

# 执行现场归档目录后缀（runner.step_archive_dir 生成 <log_id>.d/step<order>/…）
SCENE_DIR_SUFFIX = '.d'


class Command(BaseCommand):
    help = '清理超过保留期限的任务执行日志记录及落盘文件'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days', type=int, default=None,
            help='执行日志记录保留天数，默认取 TASK_LOG_RETENTION_DAYS',
        )
        parser.add_argument(
            '--file-days', type=int, default=None,
            help='落盘日志文件保留天数，默认取 TASK_LOG_FILE_RETENTION_DAYS',
        )
        parser.add_argument(
            '--files-only', action='store_true',
            help='只清理落盘日志文件，保留数据库记录',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='仅统计将删除的数量，不实际删除',
        )

    def handle(self, *args, **options):
        days = options['days'] or settings.TASK_LOG_RETENTION_DAYS
        file_days = options['file_days'] or getattr(
            settings, 'TASK_LOG_FILE_RETENTION_DAYS', days)
        dry_run = options['dry_run']
        log_root = Path(settings.TASK_LOG_DIR)

        removed_files = self._clean_files(log_root, file_days, dry_run)
        if options['files_only']:
            self.stdout.write(
                f'{"[dry-run] 将清理" if dry_run else "已清理"}落盘日志文件 '
                f'{removed_files} 个（{file_days} 天前）')
            return

        cutoff = timezone.now() - timedelta(days=days)
        qs = TaskExecutionLog.objects.filter(created_at__lt=cutoff)
        if dry_run:
            self.stdout.write(
                f'[dry-run] 将删除 {qs.count()} 条 {days} 天前的执行日志，'
                f'清理落盘文件 {removed_files} 个（{file_days} 天前）')
            return

        # 先删文件再删记录，避免记录已删而文件残留后无从索引
        for rel_path in qs.exclude(log_file='').values_list('log_file', flat=True).iterator():
            path = log_root / rel_path
            try:
                if path.exists():
                    path.unlink()
                    removed_files += 1
            except OSError:
                logger.warning('日志文件删除失败：%s', path)
        # 执行现场归档目录随记录一并清理（log_file 为空的记录也可能有归档）
        for log_id, task_id in qs.values_list('id', 'task_id').iterator():
            self._remove_scene_dir(log_root / str(task_id) / f'{log_id}.d')
        deleted, _ = qs.delete()

        self._prune_empty_dirs(log_root)
        self.stdout.write(
            f'已删除 {deleted} 条执行日志（{days} 天前），'
            f'清理落盘日志文件 {removed_files} 个（文件保留 {file_days} 天）')

    def _clean_files(self, log_root, file_days, dry_run):
        """按 mtime 清理过期落盘日志文件，返回清理数量。

        同时把仍存在但文件已删的记录 log_file 置空，避免详情页读到缺失文件。
        """
        if not log_root.is_dir():
            return 0
        deadline = time.time() - file_days * 86400
        removed, stale_rel = 0, []
        for path in log_root.rglob('*.log'):
            try:
                if path.stat().st_mtime >= deadline:
                    continue
            except OSError:
                continue
            rel = str(path.relative_to(log_root))
            if dry_run:
                removed += 1
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning('日志文件删除失败：%s', path)
                continue
            removed += 1
            stale_rel.append(rel)
            if len(stale_rel) >= 500:  # 分批清引用，避免 IN 子句过长
                TaskExecutionLog.objects.filter(log_file__in=stale_rel).update(log_file='')
                stale_rel = []
        if stale_rel:
            TaskExecutionLog.objects.filter(log_file__in=stale_rel).update(log_file='')
        removed += self._clean_scene_dirs(log_root, deadline, dry_run)
        if not dry_run:
            self._prune_empty_dirs(log_root)
        return removed

    def _clean_scene_dirs(self, log_root, deadline, dry_run):
        """按 mtime 清理过期的执行现场归档目录（<log_id>.d），返回清理数量。

        目录 mtime 取内部文件的最新值：归档在执行时一次性写入，重试会追加
        新的 step 子目录，用最新值可避免长任务的早期模块被提前清掉。
        """
        removed = 0
        for scene in log_root.glob(f'*/*{SCENE_DIR_SUFFIX}'):
            if not scene.is_dir():
                continue
            mtimes = [p.stat().st_mtime for p in scene.rglob('*') if p.is_file()]
            if mtimes and max(mtimes) >= deadline:
                continue
            removed += 1
            if not dry_run:
                self._remove_scene_dir(scene)
        return removed

    @staticmethod
    def _remove_scene_dir(scene):
        if not scene.is_dir():
            return
        try:
            shutil.rmtree(scene)
        except OSError:
            logger.warning('执行现场归档目录删除失败：%s', scene)

    @staticmethod
    def _prune_empty_dirs(log_root):
        """回收空的任务日志子目录。"""
        if not log_root.is_dir():
            return
        for sub in log_root.iterdir():
            if sub.is_dir() and not any(sub.iterdir()):
                try:
                    sub.rmdir()
                except OSError:
                    pass
