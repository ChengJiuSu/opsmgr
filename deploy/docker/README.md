# 容器化部署（Cython 编译 + docker-compose）

裸机 supervisor 部署（`deploy/*.conf` + `deploy/install.sh`）仍然可用，两条路径共存。
本文只讲容器这条。

## 1. 它由什么组成

| 文件 | 作用 |
|---|---|
| `deploy/cython/build.py` | 把 `apps/` `common/` 编成 `.so` 并删源码 |
| `deploy/docker/Dockerfile` | 多阶段：base（基线）→ builder（编译）→ runtime（无 gcc 无源码） |
| `deploy/docker/entrypoint.sh` | 一个镜像六个角色，与六个 supervisor program 一一对应 |
| `deploy/docker/init-dirs.sh` | 建宿主 bind mount 目录并设属主（首次必跑） |
| `deploy/docker/mysql/conf.d/ops.cnf` | MySQL 8.0 参数 |
| `deploy/docker/redis/redis.conf` | Redis 7.0 参数 |
| `docker-compose.yml` | 九个服务：init / web / ws / celery / celery-exec / beat /（feishu-ws）/ mysql / redis / nginx |

## 2. 首次部署

顺序不能颠倒，第 3 步漏了会得到三个看似无关的 Permission denied。

```bash
cp deploy/env.example .env                                   # 1. 填变量，见下方「必改项」
cp deploy/docker/mysql.env.example deploy/docker/mysql.env   # 2. 内置 MySQL 才需要
sudo bash deploy/docker/init-dirs.sh                         # 3. 建宿主目录 + 设属主
make image                                                   # 4. Cython 编译 + 打镜像
make frontend                                                # 5. 前端产物（nginx 直接发）
docker compose --profile mysql up -d                         # 6. 用外部库则去掉 --profile
```

### .env 必改项

- `ALLOWED_HOSTS` **必须含 `127.0.0.1`**。否则容器 healthcheck 恒得 400，web/ws 一直 unhealthy。
- `DB_HOST=mysql`（用内置库时）、`REDIS_URL=redis://:<密码>@redis:6379/0`、`REDIS_PASSWORD`。
- `APP_UID` / `APP_GID` = 宿主上 `logs/` `media/` 目录的属主（`id -u homelab`）。
  三处必须一致：`.env`、`make image` 的 build-arg、`init-dirs.sh`。
- `SECRET_ENC_KEY` 首次 `migrate` 之前必须配好（库内秘密的统一加密密钥）。
- `TASK_EXEC_BACKEND=celery`：容器化下 thread 后端没有意义（web 容器重启就带走执行中的任务）。

## 3. 日常操作

```bash
make image && docker compose up -d        # 更新（构建 + 滚动起新容器）
docker compose ps                          # 看健康状态
docker compose logs -f web                 # 只看进程自身的启动/崩溃输出
docker compose run --rm web python manage.py <cmd>   # 一次性管理命令
```

回滚：把 `.env` 的 `APP_IMAGE_TAG` 指回上一个 commit，再 `docker compose up -d`。
镜像用 `IMAGE_TAG=$(GIT_COMMIT)` 打过 tag，`make image-save` 可导出 tar 离线传。

## 4. 日志在哪

日志系统（Vector / VictoriaLogs）**不在 compose 里**，仍由宿主 systemd 托管。
容器只负责把日志写到宿主目录，采集端按文件路径照旧采：

| 内容 | 宿主路径 |
|---|---|
| Django / celery / daphne 结构化日志 | `${OPS_LOG_ROOT}/app/ops_manager-<service>.log` |
| gunicorn access（JSON） | `${OPS_LOG_ROOT}/app/gunicorn-access.log` |
| 任务执行落盘日志 | `${OPS_LOG_ROOT}/app/tasks/` |
| nginx access / error | `${OPS_LOG_ROOT}/nginx/` |
| MySQL error / slow | `${OPS_LOG_ROOT}/mysql/` |
| Redis | `${OPS_LOG_ROOT}/redis/` |

⚠ 采集端要改的只有路径变量：`vector.env` 里的 `OPS_APP_DIR` / `OPS_LOG_DIR` 指到
`${OPS_LOG_ROOT}/app`（原先是项目目录与 `~/logs`）。

⚠ 进程 stdout/stderr 走 docker 的 json-file driver（50m × 5），**不再落在
`${OPS_LOG_DIR}/opsmgr*.log`**，所以 vector.yaml 的 `proc` source 在容器化后采不到东西。
里面本来就只剩 gunicorn/celery 自身的启动与崩溃输出；要继续采得给 Vector 加
`docker_logs` source ——那会改变 stream 维度，按既定红线属于不可逆变更，单独评估再动。

⚠ MySQL / Redis 的日志走文件，`docker compose logs mysql` 是空的，排障看上表路径。

## 5. Cython 编译的边界

**编**：`apps/**`、`common/**` 的业务代码（约 150 个模块）。
**不编**：`migrations/`、`__init__.py`、`tests`、`manage.py`、`ops_manager/*`（入口与配置）。
理由写在 `deploy/cython/build.py` 头部。

已知代价，上线前要接受：

1. **提速有限**。热点是 ORM、ansible 子进程、SSH、出网 HTTP，全是 I/O。
   纯 Python 逻辑约 1.2–1.5x，端到端基本测不出。真正收益是源码不可读。
2. `makemigrations` 在生产镜像里不可用（没源码）。迁移必须开发机生成、提交仓库。
3. `inspect.getsource` 失效（本仓库无调用点）；traceback 仍有文件名与行号。
4. **Cython 版本锁在 3.2.x**。3.3.0 在 `**{f'...': v}` 这种写法上崩溃，
   本仓库有 8 个文件命中。细节见 `deploy/docker/requirements-build.txt`。
5. 编译出的 `.so` 带 ABI tag（`cpython-314-x86_64-linux-gnu`），只能在同
   Python 主次版本 + 同 libc 下跑。Dockerfile 的 base 阶段同时被 builder 和
   runtime 继承，这个约束由文件结构保证，不靠人记。

某个模块编译后行为异常时：先把它加进 `build.py` 的 `EXCLUDE_PATHS` 换回纯 Python，
再定位。**首要嫌疑是 channels 的 async 消费者**（`apps/*/consumers.py`）。

## 6. 几个不要改的地方

- **nginx 用 `network_mode: host`**。改成 bridge + ports 会让 docker 把客户端源 IP
  换成网关地址，而 API 凭证的 IP 白名单、飞书回调来源校验、`TRUSTED_PROXIES`
  三处都依赖真实 IP。故障表现是「所有白名单一律 403，报错里带一个内网网关地址」。
- **`stop_grace_period` 不要调小**。celery 930s / celery-exec 600s 与 supervisor 的
  `stopwaitsecs` 对齐：砍在中途的任务状态会永远留在 `issuing` / `running`（celery
  取出即 ack，不会重投）。
- **Redis `maxmemory-policy noeviction`**。broker 库被淘汰 = 任务凭空消失且无报错。
- **beat 只能一个实例**，多个 beat 会把同一个任务重复投递。
- **`init: true` 不要删**。ansible / acme.sh 都是子进程，PID 1 不收割就攒僵尸。
- **`feishu-ws` 与飞书回调模式互斥**，同时开着 = 同一事件处理两次（执行跑两遍）。

## 7. 还没做的

- MySQL 定时备份（`mysqldump` 侧车或宿主 cron）——容器化后最容易忘的一条。
- 镜像仓库：目前靠 `make image` 本地构建 + `make image-save` 传 tar。
- `deploy/nginx/docker-compose.yaml` 已被根 compose 取代，保留是给「后端仍在
  supervisor 上」的旧部署回退用。
