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

# channel layer 默认进程内（本地免装 Redis）；单进程 daphne + TASK_EXEC_BACKEND=thread
# 才成立，本地切 celery / 多进程时设 CHANNEL_LAYER_BACKEND=redis
CHANNEL_LAYER_BACKEND = config('CHANNEL_LAYER_BACKEND', default='memory')
CHANNEL_LAYERS = channel_layers_for(CHANNEL_LAYER_BACKEND)

# 缓存默认进程内（本地免装 Redis）；测试同样走这里，无需外部依赖。
# 本地要验限速的跨进程行为时设 CACHE_BACKEND=redis
CACHE_BACKEND = config('CACHE_BACKEND', default='memory')
CACHES = caches_for(CACHE_BACKEND)

# 开发环境日志默认 DEBUG（可通过 LOG_LEVEL 环境变量覆盖）
LOGGING['root']['level'] = config('LOG_LEVEL', default='DEBUG')
LOGGING['loggers']['django']['level'] = config('LOG_LEVEL', default='DEBUG')
LOGGING['loggers']['apps']['level'] = config('LOG_LEVEL', default='DEBUG')
