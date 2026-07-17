
"""Celery 应用定义。

仅在已安装 celery 且以 celery worker 方式运行时使用。
Web 进程通过 ops_manager/__init__.py 尝试加载，未安装 celery 时静默跳过。
"""
import os

from celery import Celery

# 默认 prod：本模块经 ops_manager/__init__.py 在 wsgi 之前被导入，
# 这里的 setdefault 先于 wsgi.py 生效——若默认 dev 会把生产进程拖回 SQLite。
# 开发环境跑 worker 时显式指定：DJANGO_SETTINGS_MODULE=ops_manager.settings.dev celery -A ops_manager worker
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ops_manager.settings.prod')

app = Celery('ops_manager')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    print(f'Request: {self.request!r}')

