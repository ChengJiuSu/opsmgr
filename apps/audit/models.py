from django.conf import settings
from django.db import models


class AuditLog(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='audit_logs',
        verbose_name='操作用户',
    )
    action = models.CharField('操作动作', max_length=20)
    path = models.CharField('请求路径', max_length=500)
    method = models.CharField('HTTP 方法', max_length=10)
    ip = models.GenericIPAddressField('客户端 IP', null=True, blank=True)
    status_code = models.PositiveSmallIntegerField('响应状态码', default=0)
    body = models.TextField('请求体', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = '操作日志'
        verbose_name_plural = '操作日志'
        db_table = 'audit_log'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.user} {self.method} {self.path}'
