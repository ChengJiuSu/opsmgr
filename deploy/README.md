# 部署指南

生产架构：nginx（前端静态 + 反向代理）+ gunicorn（Django API）+ MySQL。
任务执行默认 thread 后端，无需 Redis；切 celery 时另起 worker。

## 目录内容

| 文件 | 用途 |
|------|------|
| `env.example` | 生产环境变量模板，复制为项目根 `.env` |
| `install.sh` | **CD 主路径**：装 Jenkins 制品包，离线装依赖、迁移、重启 |
| `deploy.sh` | 无 CI 场景，首次从源码树部署：venv、联网装依赖、迁移、collectstatic、前端构建 |
| `update.sh` | 无 CI 场景，日常更新：拉代码、迁移、构建、重启服务 |
| `ops-manager.conf` | gunicorn supervisor 配置（默认托管方式） |
| `ops-manager.service` | gunicorn systemd 单元（备选托管方式） |
| `ops-manager-ws.service` | daphne systemd 单元（/ws/ ops-agent 接入，启用 agent 时部署） |
| `ops-manager-celery.conf` | celery worker supervisor 配置（`TASK_EXEC_BACKEND=celery` 时必须） |
| `ops-manager-celery.service` | celery worker systemd 单元（同上，备选托管方式） |
| `ops-manager-celery-beat.conf` | celery beat supervisor 配置（证书自动续签调度，只起一个实例） |
| `ops-manager-celery-beat.service` | celery beat systemd 单元（同上，备选托管方式） |
| `docker/` | 后端容器化：Dockerfile + entrypoint（compose 文件在仓库根） |
| `docker/Dockerfile.builder` | CI 依赖构建镜像（预编 wheelhouse / 生成 lock），不进生产 |
| `nginx.conf` | nginx 站点配置 |

## 依赖交付：CI 预编 wheelhouse，目标机离线装

后端依赖【不在目标机安装】。目标机既不需要 gcc，也不需要访问 PyPI。

```
Jenkins agent（有 docker）                       目标服务器
─────────────────────────────────────            ────────────────────────────
make lock     pip-compile   requirements.lock
make wheels   pip wheel     wheelhouse/*.whl  ─┐
make static   collectstatic staticfiles/       ├─ 打进 tar 制品 ─┐
make frontend vite build    frontend/dist/    ─┘                 │
                                                                 ▼
                                        bash deploy/install.sh
                                          python3.14 -m venv venv
                                          pip install --no-index \
                                            --find-links=wheelhouse \
                                            -r requirements.lock
                                          manage.py migrate
                                          supervisor restart
```

为什么这样切：

- **不在目标机 `pip install -r requirements.txt`**：`mysqlclient` 要编译，生产机
  就得常驻 `gcc` + `default-libmysqlclient-dev`；且每次部署都依赖外网 PyPI，
  网络抖一下部署和回滚一起失败。
- **不打整个 `venv` 进制品**：`venv/bin/*` 的 shebang 是构建机绝对路径，
  `pyvenv.cfg` 指向构建机解释器，C 扩展绑 glibc/Python ABI。能跑，但部署路径
  必须逐字符相同，换机器静默炸。
- **`requirements.lock` 必须存在**：`requirements.txt` 全是下界约束（`>=`），
  同一个 git commit 隔一周部署能解出不同版本的 Django / ansible-core / redis。
  改完 `requirements.txt` 跑 `make lock`，把锁文件一并提交。
- **构建镜像基线必须与生产机一致**（`UBUNTU_VERSION`/`PYTHON_VERSION`）：
  `mysqlclient` 的轮子链接构建机的 MySQL 客户端库，Ubuntu 是
  `libmysqlclient.so.21`、Debian 是 `libmariadb.so.3`，选错只在目标机
  `import MySQLdb` 时才暴露。`install.sh` 的依赖自检会把这种错认出来并给出提示。

### Jenkins / CD 侧约定

CI agent 需要：**docker**（跑构建镜像）。不需要 python3.14、不需要 node ——
后端命令跑在 `Dockerfile.builder` 里，前端跑在 `node:22-bookworm-slim` 里。
`make build USE_DOCKER=false` 是无 docker 时的降级路径，基线不受控，别用于正式构建。

`opsmanager.jenkinsfile` 的 `makeEnv` 可传（都有默认值，一般不用改）：

| 变量 | 默认 | 说明 |
|------|------|------|
| `SKIP_FRONTEND` | `false` | 只改后端时置 `true` 跳过前端构建 |
| `DEPLOY_ENV` | `none` | 写进 `BUILD_INFO`，部署后可核对 |
| `UBUNTU_VERSION` | `24.04` | 构建基线，**必须等于生产机版本** |
| `PYTHON_VERSION` | `3.14` | 同上；改了要重建 wheelhouse 并同步目标机 |
| `NODE_IMAGE` | `node:22-bookworm-slim` | 与 `frontend/package.json` 的 engines 对齐 |

`make build` 的 `output/` 里多了 `wheelhouse/`，共享库步骤 4/5 生成的 md5 清单
会覆盖到每个 `.whl`，即制品完整性也覆盖依赖本身（这也是不带
`--generate-hashes` 仍安全的原因）。

CD 侧（平台的任务流 / artifact_mgr 注入的 `package_*` 变量）落到目标机上是：

```bash
cd /home/ubuntu/apps/devops-manager-01
curl -fsSL "$package_url" -o /tmp/$package_name        # 带签名的下载地址
echo "$package_md5  /tmp/$package_name" | md5sum -c -
tar -xzf /tmp/$package_name -C .
bash deploy/install.sh
```

`.env`、`logs/`、`media/` 不在制品里，解包不会覆盖它们。

### 目标机一次性 bootstrap

```bash
sudo add-apt-repository -y ppa:deadsnakes/ppa && sudo apt-get update
sudo apt-get install -y python3.14 python3.14-venv libmysqlclient21
```

装完之后每次 CD 都不再动系统包。**不需要** node / npm，也不需要
`build-essential` 与 `*-dev` 包。

### 装制品

```bash
cd /home/ubuntu/apps/devops-manager-01
tar -xzf /path/to/ops-manager-<BUILD_TS>.tar.gz -C .
cp deploy/env.example .env && vim .env      # 首次；后续 .env 不随包覆盖
bash deploy/install.sh
```

`install.sh` 的开关：`--deps-only`（只装依赖）、`--no-migrate`、`--no-restart`。
多实例部署用 `SUPERVISOR_PROGRAM=ops-manager-02 bash deploy/install.sh`。

### 本地/CI 常用 make 目标

| 命令 | 作用 |
|------|------|
| `make lock` | 改完 `requirements.txt` 后重新解算 `requirements.lock`（要提交） |
| `make wheels` | 在构建镜像里预编 `wheelhouse/`，并点清数量 |
| `make build` | 完整产物到 `output/`（含 wheelhouse、staticfiles、frontend/dist） |
| `make test` | 在构建基线里跑 `manage.py test apps`（dev settings / sqlite） |
| `make smoke` | 只验证「make 落 output/ → 共享库打包」链路，不装依赖不编前端（原 `make test`） |
| `make build USE_DOCKER=false` | 无 docker 的机器上本地构建（基线不保证，仅开发用） |

## 首次部署

走 Jenkins CD 时用上面的「装制品」流程。本节是**无 CI 场景**的源码树部署
（开发机 / 应急），会联网装依赖、现编 `mysqlclient`、本机构建前端。

前置：python3.14、node22+/npm、nginx、MySQL（已建库 `ops_manager`，utf8mb4）、
`default-libmysqlclient-dev` + `pkg-config` + `build-essential`、
ansible 目标主机 SSH 免密。

```bash
git clone <repo> /opt/devops-manager
cd /opt/devops-manager
cp deploy/env.example .env && vim .env       # SECRET_KEY / ALLOWED_HOSTS / DB / SITE_BASE_URL
bash deploy/deploy.sh

# 后端托管（默认 supervisor，改路径/用户/程序名后安装）
sudo cp deploy/ops-manager.conf /etc/supervisor/conf.d/
sudo supervisorctl reread && sudo supervisorctl update

sudo cp deploy/nginx.conf /etc/nginx/conf.d/ops-manager.conf   # 改 server_name / 路径
sudo nginx -t && sudo systemctl reload nginx
```

备选 systemd 托管：

```bash
sudo cp deploy/ops-manager.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now ops-manager
```

### 启用 ops-agent（NAT 出口执行代理，可选）

agent 经 WebSocket（`/ws/agent/`）接入，需额外起 daphne（ASGI）并确保
Redis 可用（channel layer；与 celery 共用同一 `REDIS_URL`）：

```bash
sudo cp deploy/ops-manager-ws.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now ops-manager-ws
```

nginx.conf 已内置 `/ws/` → `127.0.0.1:8001` 分流；agent 侧部署见
独立仓库 ops-agent 的 README 与 `docs/ops-agent-design.md`。

部署路径与配置内路径不一致时，同步修改 `ops-manager.conf`（或 `ops-manager.service`）与 `nginx.conf`。
supervisor 程序名改动时，`update.sh` 重启用 `SUPERVISOR_PROGRAM=<程序名>` 覆盖（默认 `ops-manager-01`）。

## Docker Compose 部署（后端，可选内置 MySQL）

前置：docker + docker compose v2。前端构建产物仍由宿主机 nginx 提供
（`location ~ ^/(api|admin)/` 反代 `127.0.0.1:8000` 不变）。

⚠ 下面的 `--build` 是**在目标机上构建镜像**：要拉基础镜像、装 apt 包、编
`mysqlclient`，生产机因此变成编译机，且每次构建结果不完全可重复。这条路径
适合开发机与自用环境；正式环境请用「依赖交付」那一节的裸机制品流程，或者
在 CI 里 `docker build` 后推私有 registry，目标机只 `docker compose pull &&
docker compose up -d`（不带 `--build`）。

```bash
cd /path/to/devops-manager
cp deploy/env.example .env && vim .env
```

**方式 A：使用外部 MySQL**（`.env` 的 `DB_HOST` 指向外部库）：

```bash
docker compose up -d --build
```

**方式 B：使用内置 MySQL**（`--profile mysql` 启用，数据落 `mysql-data` 卷）：

```bash
# .env 需设：DB_HOST=mysql、DB_PASSWORD、MYSQL_ROOT_PASSWORD
docker compose --profile mysql up -d --build
```

说明：

- backend 入口自动执行 `migrate` + `collectstatic`，并带 60s 数据库重试等待
  （内置 MySQL 首次初始化较慢，无需 depends_on）。
- 挂载卷：`media`（制品）、`logs`、`ansible`（剧本目录）、`~/.ssh`（只读，
  Ansible 免密私钥）。容器内以 root 运行，SSH 私钥权限需 600。
- 管理员账号：`docker compose exec backend python manage.py createsuperuser`。
- 日常更新：`git pull && docker compose up -d --build`（前端另行 `npm run build`）；
  有 registry 时改成 `docker compose pull && docker compose up -d`。
- 镜像按 `requirements.lock` 装依赖，与裸机制品的版本集合一致。
- 查看日志：`docker compose logs -f backend`。

## 上线后配置

1. 确认数据库迁移已执行（`deploy.sh` / `update.sh` / docker entrypoint 都会自动
   跑；手工部署或只 `git pull` 没跑脚本时必须自己补，见下节）：

   ```bash
   venv/bin/python manage.py showmigrations | grep '\[ \]'   # 应无输出（未应用的迁移）
   venv/bin/python manage.py migrate --noinput
   ```

2. 浏览器登录（deploy.sh 创建的管理员账号）。
3. 「系统管理 → 系统配置」维护 Jenkins 连接：
   `jenkins.url` / `jenkins.user` / `jenkins.api_token`；
   Jenkins 在内网、manager 在公网时改配 `jenkins.agent`（见
   `docs/backend/modules/artifacts.md`）。
4. 跑一个任务验证 Ansible 与 Jenkins 连通。
5. 启用 MFA（二次认证）时按顺序做，别先拉开关：

   ```bash
   # ① .env 配 MFA_SECRET_KEY（留空会从 SECRET_KEY 派生，两者就绑死了，
   #    以后不能单独轮换；换掉它 = 已绑定用户的密钥全部解不开）
   # ② 确认丢设备兜底命令可用（最后一个管理员也丢验证器时只剩这条路）。
   #    用 --check 只读验证，别拿真账号跑真清除——会把对方已绑好的 MFA 清掉
   venv/bin/python manage.py mfa_reset <任一用户名> --check
   # ③ 确认服务器时间准确：TOTP 容差只 ±30s，时钟漂移的现象酷似「密钥不对」
   timedatectl status | grep -i ntp
   ```

   以上就绪并通知到位后，才在「系统配置」把 `mfa.enforce_scope` 从 `off` 改成
   `staff` 或 `all`。改完存量 token 因缺 `amr` 声明会被挡，所有人需重新登录一次。

## 日常更新

**走 Jenkins CD（主路径）**：解包新制品后执行

```bash
cd /home/ubuntu/apps/devops-manager-01 && bash deploy/install.sh
```

`install.sh` 依次做：校验 python 基线/`.env`/wheelhouse → 重建 venv 并
**离线**装 `requirements.lock` → 依赖 import 自检 → **`migrate --noinput`** →
重启 supervisor。不联网、不编译、不跑 `collectstatic`（静态与前端随包）。

**无 CI 场景（源码树）**：

```bash
cd /opt/devops-manager && bash deploy/update.sh
```

`update.sh` 依次做：拉代码 → `pip install -r requirements.lock` →
**`migrate --noinput`** → `collectstatic` → 前端构建 → 重启服务。

**不走脚本就必须自己补迁移**。只 `git pull` 加重启是最常见的漏项：新表没建，
接口一调就 `no such table` / `Table doesn't exist`（如 MFA 的 `auth_user_mfa`，
`LoginView` 每次登录都要查它判断是否走第二步，漏了迁移等于全站登不进）。

```bash
venv/bin/pip install -r requirements.lock       # 依赖可能也变了
venv/bin/python manage.py migrate --noinput
venv/bin/python manage.py collectstatic --noinput
```

Docker 部署无需手工执行：backend 入口每次启动都会跑 `migrate`。

## 切换 celery 任务后端（可选）

1. 装 Redis，`.env` 设 `TASK_EXEC_BACKEND=celery`、`REDIS_URL=redis://127.0.0.1:6379/0`。
2. **必须部署 worker**（缺了它任务只入队不执行，页面上是一条永远「申请中」的证书，
   且没有任何报错）。按 web 用的托管方式二选一 —— supervisor：

   ```bash
   sudo cp deploy/ops-manager-celery.conf /etc/supervisor/conf.d/
   sudo cp deploy/ops-manager-celery-beat.conf /etc/supervisor/conf.d/   # 自动续签调度，可选
   sudo supervisorctl reread && sudo supervisorctl update
   sudo supervisorctl status | grep celery
   ```

   或 systemd：

   ```bash
   sudo cp deploy/ops-manager-celery.service /etc/systemd/system/
   sudo cp deploy/ops-manager-celery-beat.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now ops-manager-celery ops-manager-celery-beat
   ```

   两边都要按实际部署改路径、运行用户、`PATH`（须含 `venv/bin` 与 `~/.acme.sh`）
   与 `HOME`；worker 与 web 用同一个运行用户，否则找不到 acme.sh、签出的文件属主也错。
   两个坑：supervisor 的续行是**缩进**不是反斜杠（写 `\` 会当字面参数传给 celery）；
   `stopwaitsecs` 默认 10s 会把正在跑的 acme.sh 砍掉，配置里已给到 930s。
3. 重启 `ops-manager` 服务。
4. 自检：`venv/bin/celery -A ops_manager inspect ping` 有回应，
   `redis-cli -n 0 llen celery` 稳定为 0。

## 常见问题

- **`no such table` / `Table ... doesn't exist`**（如 `auth_user_mfa`）：新版本带了
  迁移但没执行。跑 `venv/bin/python manage.py migrate --noinput`；用
  `showmigrations | grep '\[ \]'` 确认没有未应用的迁移。
- **登录 401 / 静态 404**：nginx `location` 路径与实际部署路径不符，或未 `collectstatic`。
- **任务执行超时被断开**：nginx `proxy_read_timeout` 与 gunicorn `--timeout` 均需 ≥600s，已在配置中给出。
- **证书一直「申请中」/ 任务点了没反应**（celery 模式）：worker 没跑。
  `venv/bin/celery -A ops_manager inspect ping` 报 `No nodes replied` +
  `redis-cli -n 0 llen celery` 不为 0 即可确认。装上
  `ops-manager-celery.conf`（supervisor）或 `ops-manager-celery.service`（systemd，见上节）。残留状态用
  `manage.py reap_stuck_cert_tasks` 收成失败；队列里的陈旧任务先
  `redis-cli -n 0 del celery` 清掉，否则 worker 一起来就把过期任务全跑一遍。
- **制品下载地址错误**：`.env` 的 `SITE_BASE_URL` 未配置为对外可达地址。
- **纯 HTTP 内网访问**：prod 配置开启了 `SESSION_COOKIE_SECURE`/`CSRF_COOKIE_SECURE`，
  API 走 JWT 不受影响；如需用 Django admin，需上 HTTPS 或临时关闭这两项。
