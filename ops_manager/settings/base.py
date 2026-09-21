"""
基础配置，所有环境共用。
敏感值通过环境变量或 .env 文件注入，不在此硬编码。
"""
from datetime import timedelta
from pathlib import Path

from decouple import config

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# ── 日志配置 ──────────────────────────────────────────────
LOGS_DIR = BASE_DIR / 'logs'
LOGS_DIR.mkdir(exist_ok=True)

LOG_LEVEL = config('LOG_LEVEL', default='INFO')

# 文件日志格式：verbose = 人读的单行文本，json = 结构化单行 JSON。
# 生产默认 json（见 settings/prod.py），因为要被 Vector 采集进 VictoriaLogs；
# 本地默认 verbose，tail 时不用过一遍 jq。控制台永远是 verbose，不受此开关影响。
LOG_FORMAT = config('LOG_FORMAT', default='verbose')
# 本进程身份，写进每条 JSON 日志的 service 字段。一台机上 gunicorn / celery /
# daphne / feishu-ws 各写各的文件，靠它在服务端区分，各 supervisor 配置里分别设。
LOG_SERVICE = config('LOG_SERVICE', default='django')

# 日志文件按进程身份分开。
# 原先所有进程都写同一个 logs/ops_manager.log：homelab 这台机上 gunicorn、
# daphne、celery-exec 三个进程各自持有一个 RotatingFileHandler 指向它，
# 轮转时各写各的 rename + 重开，**互相覆盖，轮转瞬间的日志直接消失**。
# 单机单进程时看不出来，多进程下是必然丢数据。文件名带上 service 后各写各的，
# 采集端靠 JSON 里的 service 字段照样能合起来看。
LOG_FILE = LOGS_DIR / f'ops_manager-{LOG_SERVICE}.log'
LOG_ERROR_FILE = LOGS_DIR / f'error-{LOG_SERVICE}.log'

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {name} {module}:{lineno} {message}',
            'style': '{',
            'datefmt': '%Y-%m-%d %H:%M:%S',
        },
        'json': {
            '()': 'common.logging.JSONFormatter',
            'service': LOG_SERVICE,
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
        'file': {
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': LOG_FILE,
            'maxBytes': 1024 * 1024 * 10,  # 10 MB
            'backupCount': 5,
            'formatter': LOG_FORMAT,
            'encoding': 'utf-8',
        },
        'error_file': {
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': LOG_ERROR_FILE,
            'maxBytes': 1024 * 1024 * 10,
            'backupCount': 5,
            'level': 'ERROR',
            'formatter': LOG_FORMAT,
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
    'apps.portal_mgr',
    'apps.topo_mgr',
    'apps.im_mgr',
]

# daphne 必须排在 staticfiles 之前：接管 runserver 为 ASGI（含 WebSocket）
INSTALLED_APPS = ['daphne'] + DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    # 请求 ID 必须最先入栈：它之后所有中间件（认证、审计）与视图产生的日志
    # 才带得上同一个 request_id，nginx access log 里也记着同一个值
    'common.logging.RequestIDMiddleware',
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

# ── 机读接口访问控制（API 凭证 + IP 白名单）────────────────
# 内部脚本 / Webhook 免登录调用的凭证见 apps/auth_mgr/models.ApiCredential，
# 校验链见 apps/auth_mgr/apikey.py。
#
# TRUSTED_PROXIES：本服务前置的反向代理地址（CIDR 或单 IP，逗号分隔）。
# IP 白名单校验只信这些地址转发来的 X-Forwarded-For，其余一律用 REMOTE_ADDR
# （见 common/request.auth_client_ip 的算法说明）。**多层代理必须把每一层都
# 写进来**，否则会把内层代理 IP 当成客户端，导致所有白名单失配。
# 默认只含回环：单机 nginx 反代（proxy_pass 到 127.0.0.1）即可工作；
# nginx 与应用不同机、或前面还有 SLB/WAF 时必须显式配置。
TRUSTED_PROXIES = [
    item.strip() for item in
    config('TRUSTED_PROXIES', default='127.0.0.1,::1').split(',') if item.strip()
]
# API 凭证默认限速（DRF 速率语法 `次数/周期`，周期取 s/m/h/day）。
# 单个凭证可在页面上覆盖；key 泄露时这是唯一还在拦的东西。
API_KEY_DEFAULT_RATE = config('API_KEY_DEFAULT_RATE', default='60/min')
# 飞书回调入口按来源 IP 的限速（DRF 速率语法）。验签在应用层，扫描流量到不了
# 那里就该被挡掉；nginx 的 limit_req 是同一目的的外层。定义在这里而不是下面的
# 飞书配置块：REST_FRAMEWORK 在本文件更早处引用它
FEISHU_CALLBACK_RATE = config('FEISHU_CALLBACK_RATE', default='240/min')
# 凭证校验失败的锁定策略（复用 common/ratelimit.py，按 key_id 计数）：
# 窗口内失败达阈值即锁定，锁定期内直接 429。防的是拿到 key_id 后暴力猜 secret。
API_KEY_FAIL_LIMIT = config('API_KEY_FAIL_LIMIT', default=20, cast=int)
API_KEY_FAIL_WINDOW = config('API_KEY_FAIL_WINDOW', default=300, cast=int)
API_KEY_FAIL_BLOCK = config('API_KEY_FAIL_BLOCK', default=300, cast=int)
# 凭证「最近使用」写库的最小间隔（秒）：每请求一次 UPDATE 会把机读接口的
# 写压力翻倍，这里按分钟级节流，last_used_at 有分钟级误差是可接受的
API_KEY_TOUCH_INTERVAL = config('API_KEY_TOUCH_INTERVAL', default=60, cast=int)

# ── DRF 配置 ──────────────────────────────────────────────
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        # JWTAuthentication 的子类，额外做 MFA 强制检查（放认证层而非权限层：
        # 大量 ViewSet 自带 permission_classes，全局默认权限对它们不生效）
        'apps.auth_mgr.authentication.MfaJWTAuthentication',
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
    # 只作用于显式声明了 ApiKeyThrottle 的机读接口（登录用户走 JWT，不限速）
    'DEFAULT_THROTTLE_RATES': {
        'apikey': API_KEY_DEFAULT_RATE,
        # 飞书回调（公网入口）按来源 IP 限速，见 apps/im_mgr/views.py
        'feishu_callback': FEISHU_CALLBACK_RATE,
    },
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

# ── Channels（执行日志推送 / ops-agent WebSocket 接入）────
# CHANNEL_LAYER_BACKEND 选层：
#   redis  —— 跨进程组消息，复用 celery 同一 Redis 实例。以下任一情况必须用它：
#             TASK_EXEC_BACKEND=celery、gunicorn/daphne 多进程或多实例、
#             ops-agent 的 WebSocket 与 runner 不在同一进程。
#   memory —— 进程内层，零外部依赖，仅「单个 ASGI 进程 + TASK_EXEC_BACKEND=thread」
#             可用；此时跨进程推送静默失效（前端回退轮询、agent 下发会失败）。
# dev.py 默认 memory（本地免装 Redis），prod.py 不覆盖即 redis。
CHANNEL_LAYER_BACKENDS = ('redis', 'memory')

# channels_redis 的 receive() 用 BZPOPMIN 阻塞读，阻塞时长取自库内
# RedisChannelLayer.brpop_timeout（当前 5s）。而 redis-py 8.0 起 socket_timeout
# 默认也是 5s（8.x 之前为 None 即不限，见 redis/_defaults.py），两者相等时客户端
# 读超时必然先于服务端阻塞返回触发——计时从命令发出前就开始——于是每次空闲接收
# 都抛 TimeoutError: Timeout reading from <redis>，连接被判死，异常从
# await_many_dispatch 冒出，daphne 记 "Exception inside application" 并断开
# WebSocket（此时 Redis 本身完全健康，redis-cli ping 正常）。
# 因此 socket_timeout 必须显式放宽到阻塞时长之上，不能依赖库默认值。
CHANNEL_LAYER_BRPOP_TIMEOUT = 5  # 与 channels_redis.core.RedisChannelLayer.brpop_timeout 保持一致
CHANNEL_LAYER_SOCKET_TIMEOUT = CHANNEL_LAYER_BRPOP_TIMEOUT + 15


def channel_layers_for(backend):
    """按后端名构造 CHANNEL_LAYERS（名字非法直接报错，避免静默退回 Redis）。"""
    if backend not in CHANNEL_LAYER_BACKENDS:
        raise ImproperlyConfigured(
            f'CHANNEL_LAYER_BACKEND 只能是 {"/".join(CHANNEL_LAYER_BACKENDS)}，收到 {backend!r}')
    if backend == 'memory':
        return {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}}
    return {
        'default': {
            'BACKEND': 'channels_redis.core.RedisChannelLayer',
            # hosts 传 dict 时，除 address 外的键会原样透给 redis-py 的
            # ConnectionPool.from_url（见 channels_redis.utils.create_pool）。
            'CONFIG': {
                'hosts': [{
                    'address': REDIS_URL,
                    'socket_timeout': CHANNEL_LAYER_SOCKET_TIMEOUT,
                    # 连接建立不涉及阻塞读，单独收紧，避免跟着读超时一起放大
                    'socket_connect_timeout': 5,
                    # agent 连接是长连接（nginx 侧 proxy_read_timeout 300s），
                    # 开 keepalive + 空闲探活，避免中间设备静默断链后才发现
                    'socket_keepalive': True,
                    'health_check_interval': 30,
                }],
            },
        },
    }


CHANNEL_LAYER_BACKEND = config('CHANNEL_LAYER_BACKEND', default='redis')
CHANNEL_LAYERS = channel_layers_for(CHANNEL_LAYER_BACKEND)

# ── MFA（二次认证）────────────────────────────────────────
# TOTP 密钥的加密 key（common/crypto.py）。留空则从 SECRET_KEY 派生；
# 换 key 会让已绑定用户的密钥全部解不开（需管理员重置绑定），生产建议显式
# 配置并与 SECRET_KEY 分开轮换。
MFA_SECRET_KEY = config('MFA_SECRET_KEY', default='')
# 库内可还原秘密的统一加密 key（common/crypto.py）：执行变量敏感值、连接凭据
# 密码/私钥、云账号 AccessKey Secret、Webhook 签名密钥、TOTP 密钥共用。
# 留空回退 MFA_SECRET_KEY（历史名字）再回退 SECRET_KEY。**换 key 会让已入库
# 密文全部解不开**，换前必须先导出重新加密。
SECRET_ENC_KEY = config('SECRET_ENC_KEY', default='')
# 扫码后在验证器 App 里显示的归属名
MFA_ISSUER = config('MFA_ISSUER', default='OpsManager')
# 二次认证挑战票据（登录第一步返回）的有效期（秒）：够输一次码即可
MFA_CHALLENGE_TTL = config('MFA_CHALLENGE_TTL', default=300, cast=int)
# 单张挑战票据允许的验证码错误次数，超了作废票据、要求重新输密码
MFA_MAX_ATTEMPTS = config('MFA_MAX_ATTEMPTS', default=5, cast=int)
# 恢复码数量（一次性展示，用一个作废一个）
MFA_RECOVERY_CODES = config('MFA_RECOVERY_CODES', default=10, cast=int)
# 敏感操作二次验证（sudo）窗口秒数：验一次后这段时间内的配置类写操作免重验。
# 绑当前访问令牌的 jti，换 token 即失效（见 common/sudo.py）
SUDO_TTL = config('SUDO_TTL', default=600, cast=int)

# ── 飞书接入（apps/im_mgr）──────────────────────────────────
# 开放平台域名：飞书（国内）open.feishu.cn / Lark（海外）open.larksuite.com
FEISHU_BASE_URL = config('FEISHU_BASE_URL', default='https://open.feishu.cn')
# 是否使用环境变量里的 http(s)_proxy 访问飞书。飞书在公网，多数部署需要走代理
# 才出得去，故默认 True——与 common/jumpserver.py 的 trust_env=False 相反，
# 那边的目标在内网，吃了开发机代理会得到 502
FEISHU_TRUST_ENV = config('FEISHU_TRUST_ENV', default=True, cast=bool)
# 出站请求超时（连接秒, 读取秒）：卡片回调必须 3 秒内回包，读超时不能太长
# 网页免登的授权页路径。飞书文档上这条地址换过名字（旧版是
# /open-apis/authen/v1/index），单独抽成配置项，真换了改一行就行
FEISHU_AUTHORIZE_PATH = config(
    'FEISHU_AUTHORIZE_PATH', default='/open-apis/authen/v1/authorize')
# 生产执行的二次确认票据有效期（秒）。短是刻意的：这张卡的意义是「当场确认」，
# 留久了就变成一张可以被翻出来再点一次的按钮
FEISHU_CONFIRM_TTL = config('FEISHU_CONFIRM_TTL', default=60, cast=int)
# sudo 卡片确认的有效期（秒）。比生产执行确认长一些：那张卡是「当场确认」，
# 这张要等人从网页切到飞书再切回来
FEISHU_SUDO_TTL = config('FEISHU_SUDO_TTL', default=120, cast=int)
# 双人复核的等待时长（秒）。比发起人确认那道长得多——那是「当场确认」，
# 这是要等另一个人看到消息、判断、再点
FEISHU_REVIEW_TTL = config('FEISHU_REVIEW_TTL', default=600, cast=int)
FEISHU_TIMEOUT = (
    config('FEISHU_CONNECT_TIMEOUT', default=5, cast=int),
    config('FEISHU_READ_TIMEOUT', default=10, cast=int),
)

# ── 缓存（登录失败限速等跨进程状态）───────────────────────
# CACHE_BACKEND 选层：
#   redis  —— 跨进程共享，复用 celery 同一 Redis（KEY_PREFIX 隔开键空间）。
#             生产必须用它：WSGI/daphne/celery 是各自独立的进程，限速计数必须
#             读写同一份。
#   memory —— 进程内 LocMemCache，仅本地开发与测试可用。多进程下每个 worker
#             各算一份配额，限速形同虚设（等于把阈值乘以进程数）。
# dev.py 默认 memory（本地免装 Redis），prod.py 不覆盖即 redis。
CACHE_BACKENDS = ('redis', 'memory')
# 缓存超时兜底（秒）：各调用点都显式传 timeout，这里只防漏传写成永不过期
CACHE_DEFAULT_TIMEOUT = 300


def caches_for(backend):
    """按后端名构造 CACHES（名字非法直接报错，避免静默退回 Redis）。"""
    if backend not in CACHE_BACKENDS:
        raise ImproperlyConfigured(
            f'CACHE_BACKEND 只能是 {"/".join(CACHE_BACKENDS)}，收到 {backend!r}')
    if backend == 'memory':
        return {'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'ops-manager',
            'TIMEOUT': CACHE_DEFAULT_TIMEOUT,
        }}
    return {'default': {
        'BACKEND': 'django.core.cache.backends.redis.RedisCache',
        'LOCATION': CACHE_URL,
        'KEY_PREFIX': 'opsmgr',
        'TIMEOUT': CACHE_DEFAULT_TIMEOUT,
        # OPTIONS 里除 pool_class/parser_class/serializer 外的键会原样透给
        # redis-py 的 ConnectionPool.from_url，用来给「Redis 卡住」兜底：
        # 登录路径同步读缓存，不设超时会把请求一起挂死。
        'OPTIONS': {
            'socket_timeout': 3,
            'socket_connect_timeout': 3,
        },
    }}


# 缓存 Redis 地址：默认与 celery 同库，键有 KEY_PREFIX 前缀不会串；
# 想彻底隔开就把 CACHE_URL 指到别的 db（如 redis://127.0.0.1:6379/2）
CACHE_URL = config('CACHE_URL', default=REDIS_URL)
CACHE_BACKEND = config('CACHE_BACKEND', default='redis')
CACHES = caches_for(CACHE_BACKEND)

# ── 登录失败限速（common/ratelimit.py）────────────────────
# 窗口内累计失败达阈值即锁定，锁定期内直接 429，不再校验密码。
# 按用户与按 IP 分别计数：IP 阈值刻意宽松——办公网整层人共用一个出口 IP，
# 阈值定小了一个人反复输错就能把全组挡在门外；用户阈值才是防爆破主力。
LOGIN_FAIL_LIMIT = config('LOGIN_FAIL_LIMIT', default=10, cast=int)
LOGIN_FAIL_IP_LIMIT = config('LOGIN_FAIL_IP_LIMIT', default=50, cast=int)
LOGIN_FAIL_WINDOW = config('LOGIN_FAIL_WINDOW', default=900, cast=int)
LOGIN_FAIL_BLOCK = config('LOGIN_FAIL_BLOCK', default=900, cast=int)

# ── 任务执行后端 ──────────────────────────────────────────
# thread（默认，无需 Redis）/ celery（生产，需 worker）/ sync（同步，测试用）
TASK_EXEC_BACKEND = config('TASK_EXEC_BACKEND', default='thread')
# 模块失败后等待人工处理（重试/跳过/中止）的超时秒数，超时按中止处理；
# 0 = 不等待（失败即中止）。系统配置 task.failure_wait_timeout 可运行时覆盖。
TASK_FAILURE_WAIT_TIMEOUT = config('TASK_FAILURE_WAIT_TIMEOUT', default=600, cast=int)
# 并行组内同时执行的模块数上限。不设上限时一个 N 模块的并行组会起 N 个
# ansible 进程，每个再按 task.forks（默认 10）派生 —— 5 个模块就是 50 个进程，
# 小机器上足以把 CPU 打满，连带拖慢 web。超出上限的模块排队等前面的跑完。
TASK_PARALLEL_MAX = config('TASK_PARALLEL_MAX', default=5, cast=int)

# ── 任务执行日志 ──────────────────────────────────────────
# 每次执行的完整日志流式落盘目录（输出可能含敏感回显，禁止暴露到静态文件路径）
TASK_LOG_DIR = config('TASK_LOG_DIR', default=str(BASE_DIR / 'logs' / 'tasks'))
# 执行日志记录保留天数，clean_task_logs 命令据此清理过期记录
TASK_LOG_RETENTION_DAYS = config('TASK_LOG_RETENTION_DAYS', default=90, cast=int)
# 落盘日志文件保留天数（文件体积大，比记录更早清理；含孤儿文件按 mtime 清）
TASK_LOG_FILE_RETENTION_DAYS = config('TASK_LOG_FILE_RETENTION_DAYS', default=30, cast=int)
# ansible 彩色输出：开启则日志内保留 ANSI 颜色码（前端渲染着色），关闭为纯文本
TASK_LOG_COLOR = config('TASK_LOG_COLOR', default=True, cast=bool)

# ── 构建制品（Jenkins CI 上报）───────────────────────────
# CI 上报的代码压缩包落盘根目录，按 项目组/渠道/环境/版本 分层
ARTIFACT_ROOT = config('ARTIFACT_ROOT', default=str(MEDIA_ROOT / 'artifacts'))
# 平台对外基础地址：拼接制品带签名下载 URL（CD 目标主机经此拉包），生产必须配置
SITE_BASE_URL = config('SITE_BASE_URL', default='http://127.0.0.1:8000')
# 制品下载签名密钥（Artifact.download_sig）。原来复用系统配置 artifact.api_key
# ——那把 key 同时是上报凭据，随 API 凭证上线已下线，签名必须有自己的来源。
# 留空按 SECRET_ENC_KEY → SECRET_KEY 回退（common/crypto 同一条链）。
# **换掉会让已签发但还没用掉的下载 URL 失效**：这些 URL 在执行时现算现用，
# 影响面只限于换 key 那一刻正在跑的 CD。
ARTIFACT_SIGN_KEY = config('ARTIFACT_SIGN_KEY', default='')
# 制品保留期（天）：clean_artifacts 命令据此清理过期制品记录与文件。
# 制品是「一条记录一个几百 MB 的包」，只增不减会把盘写满，而写满的表现是
# 上报 500 与任务执行一起挂（同一个磁盘），所以这个清理不是可选项。
ARTIFACT_RETENTION_DAYS = config('ARTIFACT_RETENTION_DAYS', default=90, cast=int)
# 每个 项目组/渠道/环境 组合保底保留的最新版本数，不受保留期限制。
# 为什么要保底：长期不发版的环境（如预发）里所有制品都会超期，全清掉之后
# 该环境连「回滚到上一个版本」都做不到了
ARTIFACT_KEEP_LATEST = config('ARTIFACT_KEEP_LATEST', default=3, cast=int)
# 下载交给 nginx 内部重定向（X-Accel-Redirect）的路径前缀，如 /_artifacts/。
# 为什么必须有：制品是几十~几百 MB 的包，gunicorn 是 sync worker，
# FileResponse 逐块吐包会让一个 worker 被慢客户端占住整段下载时间；叠加 nginx
# 侧的 proxy_buffering off，worker 直接按客户端带宽被拖住，4 个 worker 很容易
# 被几个下载吃光，表现就是下载中途断（nginx 日志 upstream prematurely closed）。
# 配上之后 Django 只回一个空响应带 header，文件由 nginx sendfile 直发，
# 顺带白拿 Range 断点续传。留空（开发环境）回退 FileResponse。
# nginx 侧需要一个 internal location 把该前缀 alias 到 ARTIFACT_ROOT。
ARTIFACT_XACCEL_PREFIX = config('ARTIFACT_XACCEL_PREFIX', default='')

# ── Ansible 集中管理项目 ──────────────────────────────────
# 平台方法（ansible_playbook provider）在此目录内执行既有剧本并通过 --tags 触发；
# 主机与 inventory 由该项目统一维护。ANSIBLE_INVENTORY 为默认 inventory（可留空用项目 ansible.cfg）。
ANSIBLE_PROJECT_DIR = config('ANSIBLE_PROJECT_DIR', default=str(BASE_DIR / 'ansible'))
ANSIBLE_INVENTORY = config('ANSIBLE_INVENTORY', default='')

# ── DNS 高危变更审批（cloud_mgr）──────────────────────────
# 关掉后高危变更直接执行（仍留痕、仍可回滚）。生产建议开启。
DNS_APPROVAL_ENABLED = config('DNS_APPROVAL_ENABLED', default=True, cast=bool)
# 变更单有效期（小时）：陈旧变更单被误批推上去是这类系统的典型事故。
# 另有一道执行前的漂移比对——光靠过期时间挡不住 24 小时内线上值已被别人改过的情况
DNS_APPROVAL_TTL_HOURS = config('DNS_APPROVAL_TTL_HOURS', default=24, cast=int)

# ── 证书管理（cloud_mgr，acme.sh DNS 验证签发）─────────────
# acme.sh 可执行文件路径（默认取 ~/.acme.sh/acme.sh，官方 curl 安装位置）
ACME_BIN = config('ACME_BIN', default=str(Path.home() / '.acme.sh' / 'acme.sh'))
# 证书与 acme 账号数据目录（含私钥，禁止暴露到静态文件路径）
ACME_HOME = config('ACME_HOME', default=str(MEDIA_ROOT / 'acme'))
# ACME 账号注册邮箱（Let's Encrypt 到期提醒），生产建议配置
ACME_EMAIL = config('ACME_EMAIL', default='')
# ACME CA：letsencrypt / zerossl / buypass 等 acme.sh --server 取值
ACME_SERVER = config('ACME_SERVER', default='letsencrypt')
# 签发/续签子进程超时秒数（DNS 验证含传播等待，耗时数分钟）
ACME_TIMEOUT = config('ACME_TIMEOUT', default=900, cast=int)
# TXT 记录写入后等待 DNS 传播的秒数（acme.sh --dnssleep）。
# 免费版 DNSPod/阿里云新子域传播到全部权威节点要几十秒，等不够 CA 会查到
# NXDOMAIN，并按 SOA minimum 负缓存数分钟——表现就是"第一次必失败、第二次才成功"。
# 给了 --dnssleep 后 acme.sh 不再做自己的公共 DNS 探测（那个探测查不到 CA 侧缓存）。
ACME_DNS_SLEEP = config('ACME_DNS_SLEEP', default=180, cast=int)
# 命中 DNS 传播类错误后自动重试一次前的等待秒数，要盖过 CA 侧负缓存 TTL
# （= 权威 SOA minimum，DNSPod 免费版 180s）。置 0 关闭自动重试。
ACME_DNS_RETRY_WAIT = config('ACME_DNS_RETRY_WAIT', default=200, cast=int)
# 距过期不足 N 天时 beat 自动续签（renew_expiring_certificates）
CERT_RENEW_BEFORE_DAYS = config('CERT_RENEW_BEFORE_DAYS', default=30, cast=int)

# celery beat 静态调度（需 beat 进程：celery -A ops_manager beat）
# 每 24h 扫描临期证书自动续签（interval 而非 crontab：settings 不依赖 celery 导入）；
# 未跑 beat 时可手动点续签，功能不受影响
CELERY_BEAT_SCHEDULE = {
    'renew-expiring-certificates': {
        'task': 'cloud_mgr.renew_expiring_certificates',
        'schedule': 3600 * 24,
    },
}
