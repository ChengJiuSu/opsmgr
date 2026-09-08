"""清理过期构建制品（记录 + 落盘文件 + 孤儿文件）。

制品是「一条记录一个几百 MB 的包」，只增不减会把盘写满；而磁盘写满的表现是
制品上报 500 与任务执行一起挂（同一个磁盘），所以这个清理不是可选项。

三条保护规则，任一命中即保留：

1. **未超保留期**（settings.ARTIFACT_RETENTION_DAYS，`--days` 覆盖）；
2. **每个 项目组/渠道/环境/服务类型 组合的最新 N 个版本**
   （settings.ARTIFACT_KEEP_LATEST，`--keep-latest` 覆盖）——长期不发版的环境
   里所有制品都会超期，全清掉之后连「回滚到上一个版本」都做不到。保底按**含
   服务类型**的组合算：只按三维度算的话，发版频繁的服务会把保底名额占满，另一个
   服务仅有的那个包会被清掉；
3. **被任务模块锁定的版本**（TaskStep.params.artifact_version）——那是有人显式
   指定要用的版本，清掉它下次执行会直接失败。

孤儿文件（记录已删、文件还在）按 mtime 单独扫盘清理，兜住历史上删除失败的残留。

建议挂 cron / celery beat 每日执行：

    python manage.py clean_artifacts            # 实际清理
    python manage.py clean_artifacts --dry-run  # 只看清单
"""
import logging
import os
import time
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.artifact_mgr.models import Artifact

logger = logging.getLogger('apps.artifact_mgr')


class Command(BaseCommand):
    help = '清理超过保留期的构建制品记录与文件（每个维度+服务类型组合保底保留最新若干版本）'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=None,
                            help='制品保留天数，默认取 ARTIFACT_RETENTION_DAYS')
        parser.add_argument('--keep-latest', type=int, default=None,
                            help='每个维度+服务类型组合保底保留的最新版本数，默认取 ARTIFACT_KEEP_LATEST')
        parser.add_argument('--dry-run', action='store_true',
                            help='仅列出将删除的制品，不实际删除')
        parser.add_argument('--skip-orphans', action='store_true',
                            help='跳过孤儿文件扫盘（只清记录及其文件）')

    def handle(self, *args, **options):
        days = options['days'] if options['days'] is not None else \
            getattr(settings, 'ARTIFACT_RETENTION_DAYS', 90)
        keep = options['keep_latest'] if options['keep_latest'] is not None else \
            getattr(settings, 'ARTIFACT_KEEP_LATEST', 3)
        dry_run = options['dry_run']

        expired = self._expired(days, keep)
        freed = sum(item.size for item in expired)
        for artifact in expired:
            label = (f'{artifact.project_group.code}/{artifact.channel.code}/'
                     f'{artifact.environment.code}/'
                     f'{artifact.service_type.code if artifact.service_type_id else "-"} '
                     f'{artifact.version}')
            if dry_run:
                self.stdout.write(f'  将删除 {label}（{_mb(artifact.size)}）')
                continue
            artifact.delete()
            artifact.remove_file()
            logger.info('清理过期制品：%s', label)

        orphans, orphan_bytes = 0, 0
        if not options['skip_orphans']:
            orphans, orphan_bytes = self._clean_orphans(days, dry_run)

        prefix = '[dry-run] 将' if dry_run else '已'
        self.stdout.write(self.style.SUCCESS(
            f'{prefix}删除 {len(expired)} 个过期制品（{_mb(freed)}，'
            f'保留期 {days} 天 / 每维度保底 {keep} 版），'
            f'{prefix}清理 {orphans} 个孤儿文件（{_mb(orphan_bytes)}）'
        ))

    @staticmethod
    def _expired(days, keep):
        """返回可删制品列表（已排除保底版本与被锁定版本）。"""
        cutoff = timezone.now() - timedelta(days=days)
        locked = _locked_versions()
        keep_ids = set()
        dims = Artifact.objects.values_list(
            'project_group_id', 'channel_id', 'environment_id', 'service_type_id').distinct()
        for pg, ch, env, svc in dims:
            if keep <= 0:
                break
            keep_ids.update(
                Artifact.objects.filter(
                    project_group_id=pg, channel_id=ch, environment_id=env,
                    service_type_id=svc,
                ).order_by('-id').values_list('id', flat=True)[:keep]
            )
        qs = (Artifact.objects
              .filter(reported_at__lt=cutoff)
              .exclude(id__in=keep_ids)
              .select_related('project_group', 'channel', 'environment', 'service_type')
              .order_by('id'))
        return [a for a in qs if not _is_locked(a, locked)]

    def _clean_orphans(self, days, dry_run):
        """扫 ARTIFACT_ROOT 清理「库里没有对应记录」且已过保留期的文件。

        按 mtime 判定而不是记录时间：孤儿本来就没有记录可查。保留期沿用同一个
        阈值，避免把刚上报、记录还在事务里的文件当孤儿删掉。
        """
        root = Path(getattr(settings, 'ARTIFACT_ROOT', ''))
        if not root.is_dir():
            return 0, 0
        known = set(Artifact.objects.values_list('file_path', flat=True))
        cutoff = time.time() - days * 86400
        count, freed = 0, 0
        for path in root.rglob('*'):
            if not path.is_file():
                continue
            rel = str(path.relative_to(root))
            if rel in known:
                continue
            try:
                stat = path.stat()
                if stat.st_mtime >= cutoff:
                    continue
                count, freed = count + 1, freed + stat.st_size
                if dry_run:
                    self.stdout.write(f'  孤儿文件 {rel}（{_mb(stat.st_size)}）')
                    continue
                path.unlink()
                logger.info('清理制品孤儿文件：%s', rel)
            except OSError as exc:
                logger.warning('清理制品孤儿文件失败：%s（%s）', rel, exc)
        if not dry_run:
            _prune_empty_dirs(root)
        return count, freed


def _is_locked(artifact, locked):
    """该制品是否被任务模块锁定（含服务类型口径）。

    模块没设服务类型（存量模块）时执行取包也不按服务类型收窄，等于「不限」，
    所以那条锁定要保护该维度同版本的所有服务类型的制品。
    """
    dims = (artifact.project_group_id, artifact.channel_id, artifact.environment_id)
    return ((*dims, artifact.service_type_id, artifact.version) in locked
            or (*dims, None, artifact.version) in locked)


def _locked_versions():
    """被任务模块显式锁定的 (项目组, 渠道, 环境, 服务类型, 版本) 集合。

    锁定写在 TaskStep.params.artifact_version 里（见 runner._artifact_vars），
    生效维度按模块自定义范围或任务全局维度解析、服务类型取模块自身的
    service_type——与执行时同一口径，否则会把「模块自定义范围锁定的版本」
    误判为可删。
    """
    from apps.task_exec.models import TaskStep

    locked = set()
    steps = TaskStep.objects.select_related('task').only(
        'params', 'use_custom_scope', 'service_type_id',
        'project_group_id', 'channel_id', 'environment_id',
        'task__project_group_id', 'task__channel_id', 'task__environment_id',
    )
    for step in steps:
        version = (step.params or {}).get('artifact_version')
        if not version:
            continue
        if step.use_custom_scope:
            dims = (step.project_group_id, step.channel_id, step.environment_id)
        else:
            task = step.task
            dims = (task.project_group_id, task.channel_id, task.environment_id)
        locked.add((*dims, step.service_type_id, version))
    return locked


def _prune_empty_dirs(root):
    """自底向上删掉清空后的目录（root 本身保留）。"""
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        if Path(dirpath) == root:
            continue
        try:
            if not os.listdir(dirpath):
                os.rmdir(dirpath)
        except OSError:
            pass  # 并发写入等情况跳过即可，下次清理会再试


def _mb(size):
    return f'{size / 1024 / 1024:.1f} MB'
