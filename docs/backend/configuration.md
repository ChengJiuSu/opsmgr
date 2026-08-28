# 配置说明

## 环境变量

所有敏感配置通过环境变量或 `.env` 文件注入（使用 `python-decouple`）。

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `SECRET_KEY` | `django-insecure-change-me-in-production` | Django 密钥，**生产必须修改** |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | 允许的主机名，逗号分隔 |
| `CORS_ALLOWED_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173` | 允许跨域的前端地址 |
| `JWT_ACCESS_TOKEN_LIFETIME_MINUTES` | `60` | Access Token 有效期（分钟）|
| `JWT_REFRESH_TOKEN_LIFETIME_DAYS` | `7` | Refresh Token 有效期（天）|
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | Redis 连接地址 |
| `CHANNEL_LAYER_BACKEND` | `redis`（dev 默认 `memory`）| channel layer 后端：`redis` 跨进程，`memory` 进程内免依赖 |
| `CACHE_BACKEND` | `redis`（dev 默认 `memory`）| 缓存后端：登录限速计数存这里，多进程必须 `redis` |
| `CACHE_URL` | 同 `REDIS_URL` | 缓存 Redis 地址（键带 `opsmgr` 前缀，与 celery 同库不串；要彻底隔开就换 db 号）|
| `LOGIN_FAIL_LIMIT` | `10` | 同一用户名在窗口内的失败次数上限，超限锁定 |
| `LOGIN_FAIL_IP_LIMIT` | `50` | 同一客户端 IP 的失败上限（办公网共用出口 IP，刻意宽松）|
| `LOGIN_FAIL_WINDOW` | `900` | 失败计数窗口（秒）|
| `LOGIN_FAIL_BLOCK` | `900` | 触发后的锁定时长（秒）|
| `SECRET_ENC_KEY` | 空（回退 `MFA_SECRET_KEY` 再回退 `SECRET_KEY`）| **库内可还原秘密的统一加密 key**：执行变量敏感值、TOTP 密钥等共用（`common/crypto.py`）。**换 key 会让已入库密文全部解不开**（表现为 MFA 校验失败、任务执行报变量解密失败），换前必须先导出重新加密 |
| `MFA_SECRET_KEY` | 空（回退 `SECRET_KEY`）| 历史名字（那时唯一使用者是 MFA），保留以免存量部署换名后密文解不开。新部署配 `SECRET_ENC_KEY`。TOTP 密钥的加密 key。**换掉会让已绑定用户的密钥全部解不开**（需管理员重置绑定），生产建议显式配置并与 `SECRET_KEY` 分开轮换 |
| `MFA_ISSUER` | `OpsManager` | 验证器 App 里显示的归属名 |
| `MFA_CHALLENGE_TTL` | `300` | 登录第一步返回的挑战票据有效期（秒）|
| `MFA_MAX_ATTEMPTS` | `5` | 单张票据允许的验证码错误次数，超限作废票据 |
| `MFA_RECOVERY_CODES` | `10` | 每次生成的恢复码数量 |
| `SUDO_TTL` | `600` | 敏感操作二次验证窗口（秒）：验一次后这段时间内的配置类写操作免重验 |
| `DNS_APPROVAL_ENABLED` | `True` | DNS 高危变更是否走审批。关掉后高危变更直接执行（仍留痕、仍可回滚）。生产建议开启 |
| `DNS_APPROVAL_TTL_HOURS` | `24` | 变更单有效期（小时）。陈旧变更单被误批推上去是这类系统的典型事故；另有一道执行前的漂移比对——光靠过期时间挡不住 24 小时内线上值已被别人改过 |
| `ARTIFACT_SIGN_KEY` | 空（回退 `SECRET_ENC_KEY` → `SECRET_KEY`）| 制品下载签名密钥（`Artifact.download_sig`）。原来复用已下线的 `artifact.api_key`。换掉会让已签发但还没用掉的下载 URL 失效——这些 URL 执行时现算现用，影响面只限于换 key 那一刻正在跑的 CD |
| `ARTIFACT_RETENTION_DAYS` | `90` | 制品保留天数（`clean_artifacts` 命令据此清理记录与文件）。制品是「一条记录一个几百 MB 的包」，只增不减会把盘写满，而写满的表现是上报 500 与任务执行一起挂 |
| `ARTIFACT_KEEP_LATEST` | `3` | 每个 项目组/渠道/环境 组合保底保留的最新版本数，不受保留期限制。长期不发版的环境里所有制品都会超期，全清掉之后连「回滚到上一个版本」都做不到 |
| `TRUSTED_PROXIES` | `127.0.0.1,::1` | 本服务前置的反向代理地址（CIDR 或单 IP，逗号分隔）。**API 凭证的 IP 白名单只信这些地址转发来的 `X-Forwarded-For`**；nginx 与应用不同机、或前面还有 SLB/WAF 时必须把每一层都写进来，否则白名单全部失配 |
| `API_KEY_DEFAULT_RATE` | `60/min` | API 凭证默认限速（DRF 速率语法），单个凭证可在页面覆盖 |
| `API_KEY_FAIL_LIMIT` | `20` | 凭证校验失败次数上限（按 key_id 计数），超限锁定 |
| `API_KEY_FAIL_WINDOW` | `300` | 凭证失败计数窗口（秒）|
| `API_KEY_FAIL_BLOCK` | `300` | 凭证触发后的锁定时长（秒）|
| `API_KEY_TOUCH_INTERVAL` | `60` | 凭证「最近使用」写库的最小间隔（秒），避免每请求一次 UPDATE |

---

## 多环境配置

### 开发环境（dev）

默认激活，`manage.py` 中预设：

```python
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ops_manager.settings.dev')
```

| 项目 | 配置 |
|------|------|
| 数据库 | SQLite（`db.sqlite3`，零配置）|
| DEBUG | `True` |
| CORS | 允许所有源 |
| 日志 | 控制台输出，DEBUG 级别 |
| channel layer | `InMemoryChannelLayer`（免装 Redis）|

开发环境默认不需要 Redis：`TASK_EXEC_BACKEND=thread` + 单个 daphne 进程时，
执行日志 WebSocket 推送走进程内 channel layer 即可。以下情况本地也要装 Redis 并设
`CHANNEL_LAYER_BACKEND=redis`：

- `TASK_EXEC_BACKEND=celery`（Redis 同时作为 broker/result backend）；
- 多进程运行（gunicorn 多 worker、daphne 多进程）；
- 调试 ops-agent 下发（`agent_mgr` 的 WebSocket 与 runner 不在同一进程时）。

`CACHE_BACKEND` 同理：默认 `LocMemCache` 是**进程内**的，WSGI/daphne/celery
各算一份登录失败计数，等于把阈值乘以进程数——生产必须 `redis`。缓存不可用时
限速**放行并告警**（fail-open）：运维平台正是故障时最需要能登进去的系统。

用 `memory` 层而跨了进程时不会报错，只会静默失效：日志推送收不到（前端自动回退
轮询），agent 下发返回失败。

#### Redis 层的连接参数

`redis` 层的连接参数在 `settings/base.py` 的 `channel_layers_for()` 里显式声明，
不走 redis-py 默认值。关键约束：`socket_timeout` 必须大于 channels_redis 的
`brpop_timeout`（阻塞读时长，当前 5s）。

redis-py 8.0 把 `socket_timeout` 默认值由 `None` 改成了 5s，与阻塞读时长相等时，
客户端读超时必然先触发（计时从命令发出前开始），表现为 daphne 反复抛
`TimeoutError: Timeout reading from <redis>` 并断开 agent 的 WebSocket——此时
Redis 本身是健康的，`redis-cli ping` 正常，容易误判成 Redis 故障。
`ChannelLayerConfigTests` 锁住了这个不变式。

### 生产环境（prod）

切换方式：

```bash
export DJANGO_SETTINGS_MODULE=ops_manager.settings.prod
```

生产环境需额外配置 MySQL 连接（`mysqlclient`）：

```bash
# 安装系统依赖（Ubuntu/Debian）
apt-get install -y default-libmysqlclient-dev pkg-config

# 安装 Python 包
pip install mysqlclient
```

---

## .env 文件示例

在项目根目录创建 `.env`（已添加到 `.gitignore`）：

```ini
SECRET_KEY=your-very-secret-key-here
ALLOWED_HOSTS=yourdomain.com,www.yourdomain.com
CORS_ALLOWED_ORIGINS=https://yourdomain.com
JWT_ACCESS_TOKEN_LIFETIME_MINUTES=30
JWT_REFRESH_TOKEN_LIFETIME_DAYS=7
REDIS_URL=redis://127.0.0.1:6379/0
```

---

## Django 管理后台

访问地址：`http://localhost:8000/admin/`

创建超级用户：

```bash
python manage.py createsuperuser
# 或通过 shell
python manage.py shell -c "
from apps.auth_mgr.models import User
User.objects.create_superuser('admin', 'admin@example.com', 'admin123')
"
```
