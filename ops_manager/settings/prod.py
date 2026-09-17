"""
生产环境配置：使用 MySQL，关闭 Debug，严格安全设置。
切换方式：设置环境变量 DJANGO_SETTINGS_MODULE=ops_manager.settings.prod
"""
from decouple import config

from .base import *  # noqa: F401, F403

DEBUG = False

# MySQL 生产数据库
#
# ── CONN_MAX_AGE：为什么必须显式配 ──────────────────────────
# Django 默认 0 = **每个请求新建再关闭一条 MySQL 连接**。一个页面十几个接口
# 请求就是十几次 TCP 连接 + 认证握手；平时看不出来，但 ansible 正在跑、4 个核
# 被 ansible fork 抢走时，握手延迟直接体现成「打开页面明显卡顿」。
# 复用连接后 gunicorn 每个线程各持一条：4 worker × 8 threads = 最多 32 条常驻，
# 加 daphne 与两个 celery worker，**MySQL max_connections 要 ≥ 200**
#   SHOW VARIABLES LIKE 'max_connections';
# CONN_HEALTH_CHECKS（Django 4.1+）配套开启：复用的连接可能已被 MySQL
# wait_timeout（默认 8h）单方面掐掉，不做健康检查时第一个用到它的请求会吃一个
# "MySQL server has gone away"。开了则在请求开头探活，坏连接直接换新。
# 上限别超过 MySQL wait_timeout，60s 对任何配置都安全。
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': config('DB_NAME', default='ops_manager'),
        'USER': config('DB_USER', default='root'),
        'PASSWORD': config('DB_PASSWORD', default=''),
        'HOST': config('DB_HOST', default='127.0.0.1'),
        'PORT': config('DB_PORT', default='3306'),
        'CONN_MAX_AGE': config('DB_CONN_MAX_AGE', default=60, cast=int),
        'CONN_HEALTH_CHECKS': True,
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

# ── 生产落盘格式：JSON ────────────────────────────────────────
# 被 Vector 采集进 VictoriaLogs，字段口径见 docs/log-collection-design.md。
# 设 LOG_FORMAT=verbose 可临时退回文本格式（采集端会解析失败，仅限应急排障）。
LOG_FORMAT = config('LOG_FORMAT', default='json')
for _handler in ('file', 'error_file'):
    LOGGING['handlers'][_handler]['formatter'] = LOG_FORMAT

# ── 去掉 console handler：同一行日志不要写两遍 ──────────────────
# 生产由 supervisor 接管进程的 stdout/stderr 落到 ~/logs/ops-manager.log，
# 而 file handler 同时把同样的内容写进 logs/ops_manager-<service>.log。两个文件都被
# Vector 采集 = 每条日志在 VictoriaLogs 里存两份，磁盘与查询结果双倍噪声。
# 保留 file handler（JSON、自带轮转）、去掉 console，supervisor 的那份就只剩
# gunicorn/celery 自身的启动与崩溃输出——低频且正是 file handler 覆盖不到的部分。
for _logger in (LOGGING['root'], *LOGGING['loggers'].values()):
    _logger['handlers'] = [h for h in _logger['handlers'] if h != 'console']
# django.db.backends 只挂了 console，剥掉会变成空 handlers（日志静默丢弃），
# 给它补回 file：SQL 警告本来就该留档。
LOGGING['loggers']['django.db.backends']['handlers'] = ['file']
