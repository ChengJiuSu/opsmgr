from django.apps import AppConfig


class AuthMgrConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.auth_mgr'
    label = 'auth_mgr'
    verbose_name = '用户认证'

    def ready(self):
        # 仅为触发 drf-spectacular 认证扩展的注册（靠 __init_subclass__ 登记）。
        # 不导入的话 schema 里所有接口都会显示成不需要认证，见 schema.py 头部。
        from . import schema  # noqa: F401
