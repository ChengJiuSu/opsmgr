
"""Celery 应用定义。

仅在已安装 celery 且以 celery worker 方式运行时使用。
Web 进程通过 ops_manager/__init__.py 尝试加载，未安装 celery 时静默跳过。
"""
import os

from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ops_manager.settings.dev')

app = Celery('ops_manager')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    print(f'Request: {self.request!r}')

