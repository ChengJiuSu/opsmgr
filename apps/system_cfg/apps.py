from django.apps import AppConfig


class SystemCfgConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.system_cfg'
    label = 'system_cfg'
    verbose_name = '系统配置'
