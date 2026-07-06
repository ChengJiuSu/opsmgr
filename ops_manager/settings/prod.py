"""
生产环境配置：使用 MySQL，关闭 Debug，严格安全设置。
切换方式：设置环境变量 DJANGO_SETTINGS_MODULE=ops_manager.settings.prod
"""
from decouple import config

from .base import *  # noqa: F401, F403

DEBUG = False

# MySQL 生产数据库
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': config('DB_NAME', default='ops_manager'),
        'USER': config('DB_USER', default='root'),
        'PASSWORD': config('DB_PASSWORD', default=''),
        'HOST': config('DB_HOST', default='127.0.0.1'),
        'PORT': config('DB_PORT', default='3306'),
        'OPTIONS': {
            'charset': 'utf8mb4',
            'init_command': "SET sql_mode='STRICT_TRANS_TABLES'",
        },
    }
}

# 安全配置
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

# 生产环境日志默认 WARNING（可通过 LOG_LEVEL 环境变量覆盖）
LOGGING['root']['level'] = config('LOG_LEVEL', default='WARNING')
LOGGING['loggers']['django']['level'] = config('LOG_LEVEL', default='WARNING')
LOGGING['loggers']['apps']['level'] = config('LOG_LEVEL', default='WARNING')
