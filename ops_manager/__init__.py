# 尝试加载 Celery 应用（未安装 celery 时跳过，不影响 Web 运行）。
try:
    from .celery import app as celery_app

    __all__ = ('celery_app',)
except ImportError:
    pass
