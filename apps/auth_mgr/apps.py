from django.apps import AppConfig


class AuthMgrConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.auth_mgr'
    label = 'auth_mgr'
    verbose_name = '用户认证'
