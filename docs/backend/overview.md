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
| Redis + Celery | — | 异步任务（P2 阶段启用） |

---

## 目录结构

```
devops-manager/
├── manage.py                   # Django 入口
├── requirements.txt            # 依赖清单
├── db.sqlite3                  # 开发数据库
├── ops_manager/                # 项目配置包
│   ├── settings/
│   │   ├── base.py             # 所有环境共用配置
│   │   ├── dev.py              # 开发环境配置（SQLite + DEBUG）
│   │   └── prod.py             # 生产环境配置（MySQL）
│   ├── urls.py                 # 根路由
│   ├── wsgi.py                 # WSGI 入口
│   └── asgi.py                 # ASGI 入口
├── apps/                       # 业务模块
│   ├── auth_mgr/               # 用户认证
│   ├── asset_mgr/              # 资产管理
│   ├── audit/                  # 操作审计
│   └── system_cfg/             # 系统配置
└── common/                     # 公共工具
    ├── pagination.py           # 标准分页
    └── response.py             # 统一响应 & 异常处理
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
Router → ViewSet → Serializer → ORM → Database
    │
    ▼
统一分页响应 / 统一异常响应
```

---

## 模块职责总览

| 模块 | App Label | 说明 |
|------|-----------|------|
| `apps.auth_mgr` | `auth_mgr` | 自定义用户模型、JWT 登录、Token 刷新 |
| `apps.asset_mgr` | `asset_mgr` | 服务器资产 CRUD、状态与环境过滤 |
| `apps.audit` | `audit` | 操作审计日志（中间件自动记录） |
| `apps.system_cfg` | `system_cfg` | 系统配置键值对管理 |
| `common` | — | 标准分页、统一响应格式、全局异常处理 |

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

## 相关文档

- [API 接口文档](api-reference.md)
- [数据模型](models.md)
- [配置说明](configuration.md)
- [认证模块](modules/auth.md)
- [资产管理模块](modules/assets.md)
- [审计模块](modules/audit.md)
- [系统配置模块](modules/system.md)
- [公共工具](modules/common.md)
