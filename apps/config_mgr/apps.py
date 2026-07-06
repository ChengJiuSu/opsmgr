from django.apps import AppConfig


class ConfigMgrConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.config_mgr'
    verbose_name = '配置管理'
