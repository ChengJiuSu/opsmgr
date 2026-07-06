"""
开发环境配置：使用 SQLite，开启 Debug，详细日志。
"""
from .base import *  # noqa: F401, F403

DEBUG = True

# SQLite - 开发环境零配置
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

# 开发环境允许所有跨域
CORS_ALLOW_ALL_ORIGINS = True

# 开发环境日志默认 DEBUG（可通过 LOG_LEVEL 环境变量覆盖）
LOGGING['root']['level'] = config('LOG_LEVEL', default='DEBUG')
LOGGING['loggers']['django']['level'] = config('LOG_LEVEL', default='DEBUG')
LOGGING['loggers']['apps']['level'] = config('LOG_LEVEL', default='DEBUG')
