# 后端架构总览

## 技术栈

| 组件 | 版本 | 说明 |
|------|------|------|
| Python | 3.14 | 运行时 |
| Django | 6.0.5 | Web 框架 |
| Django REST Framework | 3.17 | RESTful API |
| djangorestframework-simplejwt | 5.5 | JWT 认证 |
| drf-spectacular | 0.29 | OpenAPI/Swagger 文档 |
| django-cors-headers | 4.9 | 跨域支持 |
| SQLite / MySQL | — | 数据库（开发/生产） |
| Redis | — | Celery broker/result、Channels channel layer、限速计数缓存 |
| Celery | — | 异步任务（任务执行 `task_exec` 队列 / 证书默认队列 / beat 定时） |
| Channels + daphne | — | WebSocket：ops-agent 接入、任务日志实时推送 |

---

## 目录结构

> 进程拓扑与跨模块链路见 [全局架构图](../architecture.md)。

```
devops-manager/
├── manage.py                   # Django 入口
├── requirements.txt            # 直接依赖（requirements.lock 为钉死版本锁文件）
├── db.sqlite3                  # 开发数据库
├── ops_manager/                # 项目配置包
│   ├── settings/
│   │   ├── base.py             # 所有环境共用配置
│   │   ├── dev.py              # 开发环境配置（SQLite + DEBUG）
│   │   └── prod.py             # 生产环境配置（MySQL）
│   ├── celery.py               # Celery 应用
│   ├── urls.py                 # 根路由
│   ├── wsgi.py                 # WSGI 入口（gunicorn）
│   └── asgi.py                 # ASGI 入口（daphne，/ws/ 走 Channels）
├── apps/                       # 业务模块
│   ├── auth_mgr/               # 用户认证、角色矩阵、MFA、API 凭证
│   ├── asset_mgr/              # 资产管理（服务器 + 五类 CMDB 字典 + 服务规划）
│   ├── config_mgr/             # 归属批量分配（无模型）
│   ├── audit/                  # 操作审计
│   ├── system_cfg/             # 系统配置
│   ├── dashboard/              # 概览/值班视角聚合（无模型）
│   ├── method_mgr/             # 平台方法注册表
│   │   └── executors/          # ansible_playbook / jenkins / jumpserver / cloud_host
│   ├── task_exec/              # 任务流执行、变量、凭据、日志推送、webhook 触发
│   ├── agent_mgr/              # 执行通道（agent 接入 / 路由规则 / 指令下发）
│   ├── artifact_mgr/           # 构建制品
│   ├── cloud_mgr/              # 域名 / 解析 / 证书 / 分发
│   │   └── providers/          # 阿里云 / 腾讯云 DNS
│   ├── topo_mgr/               # 拓扑画布
│   └── portal_mgr/             # 内部导航页配置
└── common/                     # 公共工具
    ├── pagination.py           # 标准分页
    ├── response.py             # 统一响应 & 异常处理
    ├── permissions.py          # 权限档位判定
    ├── dimscope.py             # 数据范围（项目组/环境）收窄
    ├── ansible.py              # ansible / ansible-playbook 调用
    ├── jumpserver.py           # JumpServer 资产注册（直连 / 经 Agent 双通道 + 分组）
    ├── crypto.py / secrets.py  # 敏感字段加解密与脱敏
    ├── totp.py                 # MFA 一次性口令
    └── ratelimit.py            # 登录失败限速
```

---

## 请求生命周期

```
客户端请求
    │
    ▼
SecurityMiddleware          → HTTPS 强制、XSS 保护
CorsMiddleware              → 跨域头注入
SessionMiddleware           → Session 处理
CommonMiddleware            → URL 规范化
CsrfViewMiddleware          → CSRF 校验
AuthenticationMiddleware    → 用户身份解析（JWT）
MessagesMiddleware          → Flash 消息
XFrameOptionsMiddleware     → 点击劫持防护
AuditLogMiddleware          → 写操作自动审计记录
    │
    ▼
认证层  MfaJWTAuthentication  → JWT 校验 + MFA 强制（读 token 的 amr）
        ApiKeyAuthentication  → X-API-KEY：密钥比对 / 过期 / IP 白名单 / 验签 / 作用域
    │
    ▼
权限层  IsAuthenticated · IsAdmin · IsAdminOrReadOnly · IsAdminOrReadCreate
        + 角色权限矩阵 + 数据范围（common.dimscope）
    │
    ▼
Router → ViewSet → Serializer → ORM → Database
    │
    ▼
统一分页响应 / 统一异常响应
```

- **MFA 强制放认证类而非权限类**：大量 ViewSet 自带 `permission_classes`，全局默认权限对它们不生效。
- 自带 `ApiKeyAuthentication` 的机读接口（制品上报等）因此绕过 MFA —— 脚本无法过第二因子，这是期望行为。

---

## 模块职责总览

| 模块 | App Label | 说明 |
|------|-----------|------|
| `apps.auth_mgr` | `auth_mgr` | 自定义用户模型、JWT 登录/刷新、角色权限矩阵、MFA、机读接口 API 凭证 |
| `apps.asset_mgr` | `asset_mgr` | 服务器资产 CRUD、五类 CMDB 字典、服务规划（进程名/进程数） |
| `apps.config_mgr` | `config_mgr` | 服务器归属批量分配与分配概览（无模型，数据源为 asset_mgr） |
| `apps.audit` | `audit` | 操作审计日志（中间件自动记录写操作） |
| `apps.system_cfg` | `system_cfg` | 系统配置键值对管理 |
| `apps.dashboard` | `dashboard` | 概览与值班视角聚合（无模型，只读其它模块） |
| `apps.method_mgr` | `method_mgr` | 平台方法注册表 + 可插拔 provider 执行器 |
| `apps.task_exec` | `task_exec` | 任务流编排与执行、执行变量、连接凭据、日志推送、外部触发 |
| `apps.agent_mgr` | `agent_mgr` | ops-agent 接入与执行通道解析（主机三态 + 路由规则） |
| `apps.artifact_mgr` | `artifact_mgr` | 构建制品上报、签名下载、CD 制品变量注入 |
| `apps.cloud_mgr` | `cloud_mgr` | 域名、DNS 解析（风险判定/审批）、证书签发续签与分发 |
| `apps.topo_mgr` | `topo_mgr` | 拓扑画布：引用型/外部型节点、边、按维度编排 |
| `apps.portal_mgr` | `portal_mgr` | 内部导航页（/nav.html）配置 |
| `common` | — | 分页、统一响应、权限、数据范围、ansible/JumpServer 调用、加解密、TOTP、限速 |

---

## 全局 DRF 配置

```python
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': ('JWTAuthentication',),
    'DEFAULT_PERMISSION_CLASSES':     ('IsAuthenticated',),       # 全局需登录
    'DEFAULT_PAGINATION_CLASS':       'common.pagination.StandardPagination',
    'PAGE_SIZE':                      20,
    'DEFAULT_SCHEMA_CLASS':           'drf_spectacular.openapi.AutoSchema',
    'EXCEPTION_HANDLER':              'common.response.custom_exception_handler',
    'DEFAULT_RENDERER_CLASSES':       ('JSONRenderer',),
}
```

---

## 启动方式

```bash
# 激活虚拟环境
source venv/bin/activate

# 安装依赖
pip install -r requirements.txt

# 数据库迁移
python manage.py migrate

# 创建管理员
python manage.py createsuperuser

# 启动开发服务器
python manage.py runserver 0.0.0.0:8000
```

---

## 权限模型总览

四层，各管一件事，互不替代：

| 层 | 管什么 | 在哪 |
|----|--------|------|
| **认证** | 你是谁 | JWT（含 MFA 强制检查）/ API 凭证（机读） |
| **角色矩阵** | 能碰哪些模块、到什么档位 | `apps/auth_mgr/roles.py` + `module_perm()` |
| **数据范围** | 能作用在哪些项目组 / 环境上 | `common/dimscope.py`（用户与凭证共用） |
| **sudo 二次验证** | 这次操作是不是本人刚证明过 | `common/sudo.py` |

叠加关系举例：改一条 DNS 解析要同时过「登录 + `cloud` 模块 write + sudo 窗口 +
（高危时）审批」；机读上报制品要过「API Key + IP 白名单 + `artifact:report` 作用域 +
凭证数据范围」。

判定**一律在后端**。前端拿到的权限快照只用来少显示几个必然 403 的按钮。

各层的详细语义见 [认证模块](modules/auth.md)。

---

## 相关文档

- [API 接口文档](api-reference.md)
- [数据模型](models.md)
- [配置说明](configuration.md)
- [认证模块](modules/auth.md)
- [资产管理模块](modules/assets.md)
- [审计模块](modules/audit.md)
- [系统配置模块](modules/system.md)
- [公共工具](modules/common.md)
