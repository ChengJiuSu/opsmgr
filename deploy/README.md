# 部署指南

生产架构：nginx（前端静态 + 反向代理）+ gunicorn（Django API）+ MySQL。
任务执行默认 thread 后端，无需 Redis；切 celery 时另起 worker。

## 目录内容

| 文件 | 用途 |
|------|------|
| `env.example` | 生产环境变量模板，复制为项目根 `.env` |
| `deploy.sh` | 首次部署：venv、依赖、迁移、collectstatic、前端构建 |
| `update.sh` | 日常更新：拉代码、迁移、构建、重启服务 |
| `ops-manager.service` | gunicorn systemd 单元 |
| `nginx.conf` | nginx 站点配置 |

## 首次部署

前置：python3.14+、node18+/npm、nginx、MySQL（已建库 `ops_manager`，utf8mb4）、
ansible 目标主机 SSH 免密。

```bash
git clone <repo> /opt/devops-manager
cd /opt/devops-manager
cp deploy/env.example .env && vim .env       # SECRET_KEY / ALLOWED_HOSTS / DB / SITE_BASE_URL
bash deploy/deploy.sh

sudo cp deploy/ops-manager.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now ops-manager

sudo cp deploy/nginx.conf /etc/nginx/conf.d/ops-manager.conf   # 改 server_name / 路径
sudo nginx -t && sudo systemctl reload nginx
```

部署路径非 `/opt/devops-manager` 时，同步修改 `ops-manager.service` 与 `nginx.conf` 中路径。

## 上线后配置

1. 浏览器登录（deploy.sh 创建的管理员账号）。
2. 「系统管理 → 系统配置」维护 Jenkins 连接：
   `jenkins.url` / `jenkins.user` / `jenkins.api_token`。
3. 跑一个任务验证 Ansible 与 Jenkins 连通。

## 日常更新

```bash
cd /opt/devops-manager && bash deploy/update.sh
```

## 切换 celery 任务后端（可选）

1. 装 Redis，`.env` 设 `TASK_EXEC_BACKEND=celery`、`REDIS_URL=redis://127.0.0.1:6379/0`。
2. 另起 worker（可仿照 `ops-manager.service` 建 systemd 单元）：
   `venv/bin/celery -A ops_manager worker -l info`
3. 重启 `ops-manager` 服务。

## 常见问题

- **登录 401 / 静态 404**：nginx `location` 路径与实际部署路径不符，或未 `collectstatic`。
- **任务执行超时被断开**：nginx `proxy_read_timeout` 与 gunicorn `--timeout` 均需 ≥600s，已在配置中给出。
- **制品下载地址错误**：`.env` 的 `SITE_BASE_URL` 未配置为对外可达地址。
- **纯 HTTP 内网访问**：prod 配置开启了 `SESSION_COOKIE_SECURE`/`CSRF_COOKIE_SECURE`，
  API 走 JWT 不受影响；如需用 Django admin，需上 HTTPS 或临时关闭这两项。
