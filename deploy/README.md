# 部署指南

生产架构：nginx（前端静态 + 反向代理）+ gunicorn（Django API）+ MySQL。
任务执行默认 thread 后端，无需 Redis；切 celery 时另起 worker。

## 目录内容

| 文件 | 用途 |
|------|------|
| `env.example` | 生产环境变量模板，复制为项目根 `.env` |
| `deploy.sh` | 首次部署：venv、依赖、迁移、collectstatic、前端构建 |
| `update.sh` | 日常更新：拉代码、迁移、构建、重启服务 |
| `ops-manager.conf` | gunicorn supervisor 配置（默认托管方式） |
| `ops-manager.service` | gunicorn systemd 单元（备选托管方式） |
| `ops-manager-ws.service` | daphne systemd 单元（/ws/ ops-agent 接入，启用 agent 时部署） |
| `docker/` | 后端容器化：Dockerfile + entrypoint（compose 文件在仓库根） |
| `nginx.conf` | nginx 站点配置 |

## 首次部署

前置：python3.14+、node18+/npm、nginx、MySQL（已建库 `ops_manager`，utf8mb4）、
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
- 日常更新：`git pull && docker compose up -d --build`（前端另行 `npm run build`）。
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

```bash
cd /opt/devops-manager && bash deploy/update.sh
```

`update.sh` 依次做：拉代码 → `pip install -r requirements.txt` →
**`migrate --noinput`** → `collectstatic` → 前端构建 → 重启服务。

**不走脚本就必须自己补迁移**。只 `git pull` 加重启是最常见的漏项：新表没建，
接口一调就 `no such table` / `Table doesn't exist`（如 MFA 的 `auth_user_mfa`，
`LoginView` 每次登录都要查它判断是否走第二步，漏了迁移等于全站登不进）。

```bash
venv/bin/pip install -r requirements.txt        # 依赖可能也变了
venv/bin/python manage.py migrate --noinput
venv/bin/python manage.py collectstatic --noinput
```

Docker 部署无需手工执行：backend 入口每次启动都会跑 `migrate`。

## 切换 celery 任务后端（可选）

1. 装 Redis，`.env` 设 `TASK_EXEC_BACKEND=celery`、`REDIS_URL=redis://127.0.0.1:6379/0`。
2. 另起 worker（可仿照 `ops-manager.service` 建 systemd 单元）：
   `venv/bin/celery -A ops_manager worker -l info`
3. 重启 `ops-manager` 服务。

## 常见问题

- **`no such table` / `Table ... doesn't exist`**（如 `auth_user_mfa`）：新版本带了
  迁移但没执行。跑 `venv/bin/python manage.py migrate --noinput`；用
  `showmigrations | grep '\[ \]'` 确认没有未应用的迁移。
- **登录 401 / 静态 404**：nginx `location` 路径与实际部署路径不符，或未 `collectstatic`。
- **任务执行超时被断开**：nginx `proxy_read_timeout` 与 gunicorn `--timeout` 均需 ≥600s，已在配置中给出。
- **制品下载地址错误**：`.env` 的 `SITE_BASE_URL` 未配置为对外可达地址。
- **纯 HTTP 内网访问**：prod 配置开启了 `SESSION_COOKIE_SECURE`/`CSRF_COOKIE_SECURE`，
  API 走 JWT 不受影响；如需用 Django admin，需上 HTTPS 或临时关闭这两项。
