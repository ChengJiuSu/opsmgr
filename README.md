# OpsManager 运维管理后台

基于 Django + Vue 3 构建的游戏运维后台管理系统，提供服务器资产管理、操作审计、系统配置等核心功能。

---

## 功能模块

| 模块 | 说明 |
|------|------|
| 用户认证 | JWT 登录/登出、Token 自动刷新、修改密码、自定义用户模型 |
| 用户管理 | 用户 CRUD、启停、重置密码（管理员） |
| 服务器管理 | 服务器资产 CRUD，支持状态/环境/项目组/渠道/云厂商/关键字筛选与统计 |
| 执行通道 | 主机三态（自动/强制直连/指定 Agent）+ 项目组/环境路由规则，NAT 内网主机经 ops-agent 执行 |
| CMDB 基础数据 | 项目组、渠道、环境、云厂商四类字典维护，内置种子数据，被引用时删除保护 |
| 配置管理 | 服务器批量归属分配（项目组/环境/渠道/云厂商/地域），分配概览、未分配快速筛选 |
| 平台方法 | 可复用执行能力注册表：Ansible 剧本(剧本+tags)、Jenkins 构建(触发 job+传参+日志同步)，云主机管理等 provider 预留可扩展 |
| 任务执行 | 命令下发 / 调用平台方法执行，凭据管理，实时输出（均经 Ansible 调用） |
| 构建制品 | Jenkins CI 完成后脚本上报压缩包（版本/md5，关联项目组/渠道/环境），CD 执行自动注入最新制品变量 |
| 拓扑画布 | 按「项目组×渠道×环境×地域」画接入链路图：平台资源节点（主机/域名/解析/证书，实时状态）+ 外部资源节点（GA/负载均衡/CDN 手录），自动编排按维度拉平台已有资源并连出解析/落地关系，dagre 自动排版；影响面反查（主机/域名反查所在链路与上下游）、按维度克隆、导入导出 JSON；节点健康度（主机状态/Agent 在线/解析停用/证书天数）与值班模式（只读定时刷新+异常高亮），异常汇总进概览页 |
| 飞书接入 | 群里 @机器人 选任务发起执行（生产强制二次确认卡，一次性票据限本人 60s）；执行进度就地刷同一张卡；暂停确认/失败待处理直接在飞书处理（页面发起的任务会私聊推给发起人）；飞书扫码登录（视为已过二次认证，sudo 仍要验证码）；回调走 IP 白名单 + 签名 + 时间戳 + nonce 四道校验 |
| 操作审计 | 自动记录所有写操作（POST/PUT/PATCH/DELETE），多维过滤 |
| 系统配置 | 键值对形式的全局配置管理 |
| 概览首页 | 值班视角：异常（证书过期/卡死、域名与解析异常、失败执行、Agent 离线、拓扑画布异常节点）+ 待处理（暂停任务、DNS 待审批、MFA 未启用）+ 运行态 + 资产盘子（主机分布、服务规划三态、各环境当前制品版本）与近 7 天执行趋势（成功/失败分色）；区块可按人隐藏 |
| API 文档 | 自动生成 Swagger UI 文档 |

---

## 技术栈

**后端**

- Python 3.14 / Django 6.0
- Django REST Framework + JWT 认证
- SQLite（开发）/ MySQL（生产）
- drf-spectacular（Swagger 文档）

**前端**

- Vue 3 + Vite
- Element Plus（UI 组件库）
- Pinia（状态管理）+ Vue Router
- Axios（HTTP 请求）
- Vue Flow + dagre（拓扑画布与自动排版）

---

## 快速开始

### 环境要求

- Python 3.14（生产基线；轮子按主次版本编，换版本需重建 wheelhouse）
- Node.js 22（`frontend/package.json` 的 engines 已锁 `>=22 <25`）

### 后端启动

```bash
# 1. 创建并激活虚拟环境
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 2. 安装依赖
#    requirements.lock 是钉死版本的锁文件，与生产、与 CI 产物完全一致，优先用它。
#    本机装不上 mysqlclient（缺 default-libmysqlclient-dev）时，开发用 sqlite
#    并不需要它，可临时排除：
#      grep -v '^mysqlclient' requirements.lock > /tmp/req.dev && pip install -r /tmp/req.dev
pip install -r requirements.lock

# 代码风格工具另装（black / isort / pip-tools）
pip install -r requirements-dev.txt

# 3. 数据库迁移
python manage.py migrate   # 拉取新代码后若带了新迁移，需重跑

# 4. 创建管理员账号
python manage.py createsuperuser

# 5. 启动服务
python manage.py runserver 0.0.0.0:8000
```

### 前端启动

```bash
cd frontend

# 安装依赖
npm install

# 启动开发服务器
npm run dev
```

启动后访问：

| 地址 | 说明 |
|------|------|
| http://localhost:5173 | 前端页面 |
| http://localhost:8000/api/docs/ | Swagger API 文档 |
| http://localhost:8000/admin/ | Django 管理后台 |

---

## 项目结构

> 全局架构图（进程拓扑、请求链路、模块依赖、执行/云控时序）见 **[docs/architecture.md](docs/architecture.md)**。

```
devops-manager/
├── manage.py
├── requirements.txt / requirements.lock   # 直接依赖 / 钉死版本锁文件
├── ops_manager/              # Django 项目配置
│   ├── settings/
│   │   ├── base.py           # 通用配置
│   │   ├── dev.py            # 开发环境（SQLite）
│   │   └── prod.py           # 生产环境（MySQL）
│   ├── celery.py             # Celery 应用（broker/result = Redis）
│   ├── urls.py               # 根路由
│   ├── wsgi.py               # WSGI 入口（gunicorn，全部 HTTP API）
│   └── asgi.py               # ASGI 入口（daphne，/ws/ 走 Channels）
├── apps/                     # 业务模块
│   ├── auth_mgr/             # 用户认证与管理、角色矩阵、MFA、API 凭证
│   ├── asset_mgr/            # 资产管理（CMDB：服务器/服务规划 + 五类字典）
│   ├── config_mgr/           # 配置管理（服务器归属批量分配，无模型，数据源为 asset_mgr）
│   ├── audit/                # 操作审计（中间件自动记录写操作）
│   ├── system_cfg/           # 系统配置
│   ├── dashboard/            # 概览/值班视角聚合（无模型）
│   ├── method_mgr/           # 平台方法（provider 执行器可扩展）
│   │   └── executors/        # ansible_playbook / jenkins / jumpserver / cloud_host(预留)
│   ├── task_exec/            # 任务执行（任务流、变量、凭据、日志推送、webhook 触发）
│   ├── agent_mgr/            # 执行通道（ops-agent 接入、路由规则、指令下发）
│   ├── artifact_mgr/         # 构建制品（上报、签名下载、CD 变量注入）
│   ├── cloud_mgr/            # 云控（域名、解析审批、证书签发与分发）
│   │   └── providers/        # 阿里云 / 腾讯云 DNS（零 SDK，直连 OpenAPI）
│   ├── topo_mgr/             # 拓扑画布（画布/节点/边）
│   ├── im_mgr/               # 飞书接入（回调验签、卡片交互、SSO、进度回显、绑定）
│   └── portal_mgr/           # 内部导航页配置
├── common/                   # 公共工具（分页、响应、权限、数据范围、ansible、
│                             #   jumpserver、加解密、TOTP、限速）
├── frontend/                 # Vue 3 前端
│   └── src/
│       ├── api/              # 接口调用层
│       ├── stores/           # Pinia 状态管理
│       ├── router/           # 路由配置
│       ├── utils/            # Axios 封装、任务日志 WebSocket 流
│       └── views/            # 页面组件
├── ansible/                  # 内置剧本示例与 inventory
├── deploy/                   # 部署产物：supervisor 配置、nginx 模板、安装/更新脚本
└── docs/                     # 项目文档
    ├── architecture.md       # 全局架构图
    ├── ops-agent-design.md   # ops-agent 通道设计
    ├── backend/              # 后端文档
    └── frontend/             # 前端文档
```

---

## 环境变量

在项目根目录创建 `.env` 文件（开发环境可选，生产环境必须配置）：

```ini
SECRET_KEY=your-secret-key-here
ALLOWED_HOSTS=localhost,127.0.0.1
CORS_ALLOWED_ORIGINS=http://localhost:5173
JWT_ACCESS_TOKEN_LIFETIME_MINUTES=60
JWT_REFRESH_TOKEN_LIFETIME_DAYS=7
REDIS_URL=redis://127.0.0.1:6379/0
# 缓存后端：登录失败限速的计数存这里，多进程部署必须 redis（dev 默认 memory）
CACHE_BACKEND=redis
```

---

## API 概览

所有接口需在请求头携带 Token（登录接口除外）：

```
Authorization: Bearer <access_token>
```

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/auth/login/` | 登录 |
| POST | `/api/auth/logout/` | 登出（拉黑 refresh token） |
| POST | `/api/auth/token/refresh/` | 刷新 Token |
| GET/PATCH | `/api/auth/me/` | 当前用户信息 / 更新个人资料 |
| POST | `/api/auth/change-password/` | 修改密码 |
| GET/POST | `/api/auth/users/` | 用户列表 / 创建（仅管理员） |
| GET/PATCH/DELETE | `/api/auth/users/{id}/` | 用户详情 / 更新 / 删除（仅管理员） |
| POST | `/api/auth/users/{id}/toggle_active/` | 启用 / 停用用户 |
| POST | `/api/auth/users/{id}/reset_password/` | 重置用户密码 |
| GET/POST | `/api/assets/servers/` | 服务器列表 / 创建（支持环境/项目组/渠道/云厂商按编码或 id 过滤） |
| GET/PUT/DELETE | `/api/assets/servers/{id}/` | 服务器详情 / 更新 / 删除 |
| GET | `/api/assets/servers/stats/` | 服务器状态 / 环境 / 项目组分组统计 |
| GET/POST | `/api/assets/project-groups/` | 项目组列表 / 创建（CMDB 字典，管理员可写） |
| GET/POST | `/api/assets/channels/` | 渠道列表 / 创建（CMDB 字典，管理员可写） |
| GET/POST | `/api/assets/environments/` | 环境列表 / 创建（CMDB 字典，管理员可写） |
| GET/POST | `/api/assets/cloud-providers/` | 云厂商列表 / 创建（CMDB 字典，管理员可写） |
| POST | `/api/config/allocation/assign/` | 服务器批量归属分配（仅管理员，支持设置/清空/保持不变） |
| GET | `/api/config/allocation/overview/` | 分配概览（各维度未分配数量与分布） |
| GET | `/api/audit/logs/` | 操作审计日志（支持 username/method/status_code/keyword/date 过滤） |
| GET/POST | `/api/system/configs/` | 系统配置列表 / 创建 |
| GET | `/api/dashboard/stats/` | 概览聚合统计（服务器 / 用户 / 审计 / 近 7 天趋势） |
| GET | `/api/dashboard/overview/` | 值班视角聚合（异常 / 待处理 / 运行态 / 资产分布 / 各环境当前版本 / 近 7 天执行趋势，块级按角色矩阵裁剪） |
| GET/POST | `/api/exec/methods/` | 平台方法列表 / 创建（仅管理员，普通用户只读） |
| GET/PATCH/DELETE | `/api/exec/methods/{id}/` | 方法详情 / 更新 / 删除 |
| GET/POST | `/api/exec/tasks/` | 执行任务列表 / 创建（创建即调度执行） |
| GET/DELETE | `/api/exec/tasks/{id}/` | 任务详情 / 删除 |
| POST | `/api/exec/tasks/{id}/rerun/` | 重新执行任务 |
| GET | `/api/exec/tasks/{id}/output/` | 轮询任务状态与输出 |
| GET/POST | `/api/exec/credentials/` | 连接凭据列表 / 创建（仅管理员） |
| GET/PUT | `/api/feishu/app/` | 飞书应用配置（system write + sudo，密钥不回显） |
| GET/POST | `/api/feishu/channels/` | 飞书会话白名单 |
| GET/POST/DELETE | `/api/feishu/bindings/` | 飞书绑定关系（account write + sudo） |
| GET/POST | `/api/feishu/sso/*` | 飞书登录：状态 / 授权地址 / 授权码换令牌 / 我的绑定 |
| POST | `/api/feishu/callback/<slug>/{event,card}/` | 飞书回调（免登录，走签名校验，恒定回包） |

完整接口文档见 [docs/backend/api-reference.md](docs/backend/api-reference.md) 或访问 `/api/docs/`。

---

## 平台方法与任务执行

任务分两类，均通过 Ansible 下发执行：

**1. 命令任务（ad-hoc）**

基于所选服务器资产生成临时动态 inventory（本机地址自动用 `local` 连接），
以 shell 模块执行：`ansible <pattern> -i <动态 inventory> -m shell -a "<命令>"`。
登录信息来自服务器的 SSH 用户/端口及可选的「连接凭据」（密码或私钥，密钥/密码不在 API 回显）。

**2. 平台方法任务（method）**

「平台方法」是集中管理的可复用执行能力注册表，通过可插拔的 **provider 执行器**扩展：

| provider | 说明 |
|----------|------|
| `ansible_playbook` | 一个方法 = 集中管理的 Ansible 项目中的**剧本文件 + 一组 tags**；执行 `ansible-playbook <剧本> --tags <tags>`（在 `ANSIBLE_PROJECT_DIR` 内运行，inventory 由项目维护） |
| `cloud_host` | 云主机管理（预留），后续对接云平台 API |

- 平台方法在「方法管理」中维护（管理员），执行时选择方法即可；`tags` / `limit` / `extra-vars` / `inventory` 可在方法上设默认值，并支持任务级覆盖。
- 剧本路径带**路径穿越防护**，不允许越出 `ANSIBLE_PROJECT_DIR`。
- 新增 provider：继承 `apps/method_mgr/executors/base.py:BaseExecutor`，用 `@register('<provider>')` 装饰并在 `executors/__init__.py` 导入一次即可被调度发现。

集中管理的 Ansible 项目目录由环境变量配置：

```ini
ANSIBLE_PROJECT_DIR=/path/to/your/ansible-project   # 剧本/inventory/roles 集中维护
ANSIBLE_INVENTORY=inventory/prod                    # 默认 inventory，可留空用项目 ansible.cfg
```

执行后端由 `TASK_EXEC_BACKEND` 控制：

| 取值 | 说明 |
|------|------|
| `thread`（默认） | 后台线程异步执行，无需 Redis/Celery，适合开发与小规模 |
| `celery` | 投递到 Celery worker，适合生产高并发 |
| `sync` | 同步执行，主要用于测试 |

使用 Celery 后端时需安装并启动 worker：

```bash
pip install celery redis
celery -A ops_manager worker -l info
```

> 前置依赖：执行机需安装 `ansible-core`（提供 `ansible` / `ansible-playbook`），
> 并对目标主机具备 SSH 连通性。

---

## 文档

- [全局架构图](docs/architecture.md)
- [后端文档](docs/backend/README.md)
- [前端文档](docs/frontend/README.md)

