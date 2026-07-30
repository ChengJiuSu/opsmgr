"""
基础配置，所有环境共用。
敏感值通过环境变量或 .env 文件注入，不在此硬编码。
"""
from datetime import timedelta
from pathlib import Path

from decouple import config

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# ── 日志配置 ──────────────────────────────────────────────
LOGS_DIR = BASE_DIR / 'logs'
LOGS_DIR.mkdir(exist_ok=True)

LOG_LEVEL = config('LOG_LEVEL', default='INFO')

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {name} {module}:{lineno} {message}',
            'style': '{',
            'datefmt': '%Y-%m-%d %H:%M:%S',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
        'file': {
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': LOGS_DIR / 'ops_manager.log',
            'maxBytes': 1024 * 1024 * 10,  # 10 MB
            'backupCount': 5,
            'formatter': 'verbose',
            'encoding': 'utf-8',
        },
        'error_file': {
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': LOGS_DIR / 'error.log',
            'maxBytes': 1024 * 1024 * 10,
            'backupCount': 5,
            'level': 'ERROR',
            'formatter': 'verbose',
            'encoding': 'utf-8',
        },
    },
    'root': {
        'handlers': ['console', 'file'],
        'level': LOG_LEVEL,
    },
    'loggers': {
        'django': {
            'handlers': ['console', 'file', 'error_file'],
            'level': LOG_LEVEL,
            'propagate': False,
        },
        # 避免 SQL 查询刷屏，只记录警告及以上
        'django.db.backends': {
            'handlers': ['console'],
            'level': 'WARNING',
            'propagate': False,
        },
        'django.request': {
            'handlers': ['console', 'file', 'error_file'],
            'level': 'WARNING',
            'propagate': False,
        },
        'apps': {
            'handlers': ['console', 'file', 'error_file'],
            'level': LOG_LEVEL,
            'propagate': False,
        },
    },
}

SECRET_KEY = config('SECRET_KEY', default='django-insecure-change-me-in-production')

ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='localhost,127.0.0.1').split(',')

# 内置应用
DJANGO_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
]

# 第三方应用
THIRD_PARTY_APPS = [
    'channels',
    'rest_framework',
    'rest_framework_simplejwt',
    'rest_framework_simplejwt.token_blacklist',
    'corsheaders',
    'drf_spectacular',
]

# 本地应用
LOCAL_APPS = [
    'apps.auth_mgr',
    'apps.asset_mgr',
    'apps.config_mgr',
    'apps.audit',
    'apps.system_cfg',
    'apps.dashboard',
    'apps.method_mgr',
    'apps.task_exec',
    'apps.artifact_mgr',
    'apps.agent_mgr',
    'apps.cloud_mgr',
]

# daphne 必须排在 staticfiles 之前：接管 runserver 为 ASGI（含 WebSocket）
INSTALLED_APPS = ['daphne'] + DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    # 操作审计中间件（放在认证中间件之后）
    'apps.audit.middleware.AuditLogMiddleware',
]

ROOT_URLCONF = 'ops_manager.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'ops_manager.wsgi.application'
ASGI_APPLICATION = 'ops_manager.asgi.application'

# 使用自定义用户模型
AUTH_USER_MODEL = 'auth_mgr.User'

# 密码校验
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# 国际化
LANGUAGE_CODE = 'zh-hans'
TIME_ZONE = 'Asia/Shanghai'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ── DRF 配置 ──────────────────────────────────────────────
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
    'DEFAULT_PAGINATION_CLASS': 'common.pagination.StandardPagination',
    'PAGE_SIZE': 20,
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    'EXCEPTION_HANDLER': 'common.response.custom_exception_handler',
    'DEFAULT_RENDERER_CLASSES': (
        'rest_framework.renderers.JSONRenderer',
    ),
}

# ── JWT 配置 ──────────────────────────────────────────────
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(
        minutes=config('JWT_ACCESS_TOKEN_LIFETIME_MINUTES', default=60, cast=int)
    ),
    'REFRESH_TOKEN_LIFETIME': timedelta(
        days=config('JWT_REFRESH_TOKEN_LIFETIME_DAYS', default=7, cast=int)
    ),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'AUTH_HEADER_TYPES': ('Bearer',),
    'TOKEN_OBTAIN_SERIALIZER': 'apps.auth_mgr.serializers.CustomTokenObtainPairSerializer',
}

# ── API 文档配置 ──────────────────────────────────────────
SPECTACULAR_SETTINGS = {
    'TITLE': 'OpsManager 运维后台 API',
    'DESCRIPTION': '游戏运维后台管理系统 API 文档',
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
}

# ── CORS 配置 ─────────────────────────────────────────────
CORS_ALLOWED_ORIGINS = config(
    'CORS_ALLOWED_ORIGINS',
    default='http://localhost:5173,http://127.0.0.1:5173',
).split(',')
CORS_ALLOW_CREDENTIALS = True

# ── Redis / Celery 配置（P2 启用）────────────────────────
REDIS_URL = config('REDIS_URL', default='redis://127.0.0.1:6379/0')
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_TIMEZONE = 'Asia/Shanghai'

# ── Channels（ops-agent WebSocket 接入）──────────────────
# Redis channel layer：AgentConsumer（ASGI 进程）与 runner（WSGI/celery 进程）
# 跨进程通信必需；复用 celery 同一 Redis 实例。
CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels_redis.core.RedisChannelLayer',
        'CONFIG': {'hosts': [REDIS_URL]},
    },
}

# ── 任务执行后端 ──────────────────────────────────────────
# thread（默认，无需 Redis）/ celery（生产，需 worker）/ sync（同步，测试用）
TASK_EXEC_BACKEND = config('TASK_EXEC_BACKEND', default='thread')
# 模块失败后等待人工处理（重试/跳过/中止）的超时秒数，超时按中止处理；
# 0 = 不等待（失败即中止）。系统配置 task.failure_wait_timeout 可运行时覆盖。
TASK_FAILURE_WAIT_TIMEOUT = config('TASK_FAILURE_WAIT_TIMEOUT', default=600, cast=int)

# ── 任务执行日志 ──────────────────────────────────────────
# 输出超过预览上限时全量落盘的目录（输出可能含敏感回显，禁止暴露到静态文件路径）
TASK_LOG_DIR = config('TASK_LOG_DIR', default=str(BASE_DIR / 'logs' / 'tasks'))
# 执行日志保留天数，clean_task_logs 命令据此清理过期记录与文件
TASK_LOG_RETENTION_DAYS = config('TASK_LOG_RETENTION_DAYS', default=90, cast=int)

# ── 构建制品（Jenkins CI 上报）───────────────────────────
# CI 上报的代码压缩包落盘根目录，按 项目组/渠道/环境/版本 分层
ARTIFACT_ROOT = config('ARTIFACT_ROOT', default=str(MEDIA_ROOT / 'artifacts'))
# 平台对外基础地址：拼接制品带签名下载 URL（CD 目标主机经此拉包），生产必须配置
SITE_BASE_URL = config('SITE_BASE_URL', default='http://127.0.0.1:8000')

# ── Ansible 集中管理项目 ──────────────────────────────────
# 平台方法（ansible_playbook provider）在此目录内执行既有剧本并通过 --tags 触发；
# 主机与 inventory 由该项目统一维护。ANSIBLE_INVENTORY 为默认 inventory（可留空用项目 ansible.cfg）。
ANSIBLE_PROJECT_DIR = config('ANSIBLE_PROJECT_DIR', default=str(BASE_DIR / 'ansible'))
ANSIBLE_INVENTORY = config('ANSIBLE_INVENTORY', default='')
