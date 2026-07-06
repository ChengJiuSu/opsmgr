from django.db import models


class SystemConfig(models.Model):
    key = models.CharField('配置项', max_length=100, unique=True)
    value = models.TextField('配置值')
    description = models.CharField('说明', max_length=255, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = '系统配置'
        verbose_name_plural = '系统配置'
        db_table = 'system_config'

    def __str__(self):
        return self.key
