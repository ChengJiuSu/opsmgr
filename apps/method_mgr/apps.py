from django.apps import AppConfig


class MethodMgrConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.method_mgr'
    verbose_name = '平台方法管理'

    def ready(self):
        # 触发各 provider 执行器注册
        from . import executors  # noqa: F401
