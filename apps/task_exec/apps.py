from django.apps import AppConfig


class TaskExecConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.task_exec'
    verbose_name = '任务执行'
