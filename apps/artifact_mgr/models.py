"""构建制品：Jenkins CI 完成后上报的代码压缩包。

制品按 项目组/渠道/环境/版本 唯一定位，文件落盘 ARTIFACT_ROOT
（{项目组code}/{渠道code}/{环境code}/{版本}/{原始文件名}），DB 记录版本与 md5。
CD 执行时 task_exec.runner 按模块有效维度取最新（或锁定版本）制品，
把 package_url/package_version/package_md5 注入 --extra-vars。
"""
import hashlib
import hmac
import os
import re

from django.conf import settings
from django.db import models

# 版本号与文件名白名单：仅字母数字 . _ -，从源头排除路径穿越
VERSION_RE = re.compile(r'^[A-Za-z0-9._-]{1,100}$')
FILENAME_RE = re.compile(r'^[A-Za-z0-9._-]{1,255}$')


def _api_key():
    """制品上报/下载签名共用的 API Key：系统配置优先，环境变量兜底。"""
    from apps.system_cfg.services import get_config

    return get_config('artifact.api_key') or os.environ.get('ARTIFACT_API_KEY', '')


class Artifact(models.Model):
    """构建制品记录。只能由 CI 上报（或后续手工导入），平台侧只读/删除。"""

    project_group = models.ForeignKey(
        'asset_mgr.ProjectGroup', verbose_name='项目组', on_delete=models.PROTECT, related_name='artifacts')
    channel = models.ForeignKey(
        'asset_mgr.Channel', verbose_name='渠道', on_delete=models.PROTECT, related_name='artifacts')
    environment = models.ForeignKey(
        'asset_mgr.Environment', verbose_name='环境', on_delete=models.PROTECT, related_name='artifacts')
    version = models.CharField('版本号', max_length=100)
    # 相对 ARTIFACT_ROOT 的落盘路径；不用 FileField，便于流式写盘同时计算 md5
    file_path = models.CharField('文件路径', max_length=500)
    original_name = models.CharField('原始文件名', max_length=255)
    md5 = models.CharField('MD5', max_length=32)
    size = models.BigIntegerField('文件大小(字节)', default=0)
    jenkins_job = models.CharField('Jenkins Job', max_length=150, blank=True)
    build_number = models.PositiveIntegerField('构建编号', null=True, blank=True)
    source = models.CharField('上报来源', max_length=30, default='jenkins')
    remark = models.CharField('备注', max_length=255, blank=True)
    reported_at = models.DateTimeField('上报时间', auto_now_add=True)

    class Meta:
        verbose_name = '构建制品'
        verbose_name_plural = '构建制品'
        db_table = 'artifact'
        ordering = ['-id']
        constraints = [
            models.UniqueConstraint(
                fields=['project_group', 'channel', 'environment', 'version'],
                name='uniq_artifact_dims_version',
            ),
        ]

    def __str__(self):
        return f'{self.project_group_id}/{self.channel_id}/{self.environment_id} {self.version}'

    @classmethod
    def latest_for(cls, project_group_id, channel_id, environment_id, version=None):
        """按三维度精确匹配取制品：version 为空取最新一条，指定则精确取该版本。

        供 CD 执行时注入制品变量（task_exec.runner._artifact_vars）调用。
        """
        qs = cls.objects.filter(
            project_group_id=project_group_id,
            channel_id=channel_id,
            environment_id=environment_id,
        )
        if version:
            return qs.filter(version=version).first()
        return qs.order_by('-id').first()

    def abs_path(self):
        return os.path.join(settings.ARTIFACT_ROOT, self.file_path)

    def download_sig(self):
        """下载签名：hmac_sha256(api_key, "{id}:{md5}")。

        注入到 CD 变量的 package_url 携带该签名，目标主机免登录拉包，
        原始 API Key 不出现在 URL 与执行日志中。
        """
        key = _api_key()
        if not key:
            return ''
        return hmac.new(key.encode(), f'{self.id}:{self.md5}'.encode(), hashlib.sha256).hexdigest()

    def download_url(self):
        """带签名的绝对下载地址（SITE_BASE_URL 拼 host，供无 request 上下文的注入场景）。"""
        base = settings.SITE_BASE_URL.rstrip('/')
        return f'{base}/api/artifacts/{self.id}/download/?sig={self.download_sig()}'
