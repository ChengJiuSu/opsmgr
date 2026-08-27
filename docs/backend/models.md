# 数据模型

## ER 关系图

```
auth_mgr.User ──┐
                │  FK (on_delete=SET_NULL)
                └──► audit.AuditLog

asset_mgr.ProjectGroup ──┐
asset_mgr.Channel ───────┤  FK (on_delete=PROTECT, 可空)
asset_mgr.Environment ───┼──► asset_mgr.Server
asset_mgr.CloudProvider ─┘

agent_mgr.AgentNode ──── FK (SET_NULL, 可空) ──► asset_mgr.Server.agent
agent_mgr.AgentNode ──── FK (CASCADE) ────────► agent_mgr.AgentRoute
asset_mgr.ProjectGroup ─ FK (PROTECT) ────────► agent_mgr.AgentRoute
asset_mgr.Environment ── FK (PROTECT, 可空) ──► agent_mgr.AgentRoute

auth_mgr.User    独立
system_cfg.SystemConfig 独立
```

---

## User（用户）

**App：** `auth_mgr` | **表名：** `auth_user`

继承自 Django `AbstractUser`，在标准字段基础上扩展了运维相关字段。

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `id` | BigAutoField | PK | 主键 |
| `username` | CharField(150) | — | 用户名，唯一 |
| `password` | CharField | — | 加密密码 |
| `email` | EmailField | — | 邮箱 |
| `first_name` | CharField(150) | — | 名 |
| `last_name` | CharField(150) | — | 姓 |
| `is_active` | BooleanField | True | 账号是否可用 |
| `is_staff` | BooleanField | False | 可登录 Admin |
| `is_superuser` | BooleanField | False | 超级管理员 |
| `date_joined` | DateTimeField | now | 注册时间 |
| `last_login` | DateTimeField | null | 最后登录时间 |
| `phone` | CharField(20) | '' | 手机号（扩展）|
| `department` | CharField(100) | '' | 所属部门（扩展）|
| `is_ldap` | BooleanField | False | 是否 LDAP 账号（扩展）|

---

## CMDB 字典表（asset_mgr）

四张字典表继承抽象基类 `DictBase`，公共字段：

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `id` | BigAutoField | PK | 主键 |
| `name` | CharField(100) | — | 名称 |
| `code` | SlugField(50) | — | 唯一编码（字母/数字/中划线/下划线）|
| `enabled` | BooleanField | True | 是否启用 |
| `sort_order` | PositiveIntegerField | 0 | 排序 |
| `remark` | TextField | '' | 备注 |
| `created_at` / `updated_at` | DateTimeField | auto | 时间戳 |

各表差异字段与表名：

| 模型 | 表名 | 差异字段 |
|------|------|------|
| `ProjectGroup` 项目组 | `asset_project_group` | `owner` 负责人、`contact` 联系方式 |
| `Channel` 渠道 | `asset_channel` | `platform`：`all` / `android` / `ios` |
| `Environment` 环境 | `asset_environment` | `is_prod` 是否生产环境 |
| `CloudProvider` 云厂商 | `asset_cloud_provider` | — |

**默认排序：** `sort_order, id`；**内置种子数据**由迁移 `0004` 写入
（环境 dev/test/prod、云厂商 aliyun/tencent/huawei/aws/gcp/idc、常用渠道六个）。

---

## Server（服务器）

**App：** `asset_mgr` | **表名：** `asset_server`

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `id` | BigAutoField | PK | 主键 |
| `name` | CharField(100) | — | 服务器名称 |
| `ip` | GenericIPAddressField | — | IP 地址（IPv4/IPv6）|
| `os` | CharField(50) | '' | 操作系统 |
| `cpu` | PositiveSmallIntegerField | 0 | CPU 核数 |
| `memory` | PositiveIntegerField | 0 | 内存（MB）|
| `status` | CharField(20) | 'running' | 状态（枚举）|
| `project_group` | FK → ProjectGroup | null | 项目组（PROTECT）|
| `channel` | FK → Channel | null | 渠道（PROTECT）|
| `environment` | FK → Environment | null | 环境（PROTECT）|
| `cloud_provider` | FK → CloudProvider | null | 云厂商（PROTECT）|
| `region` | CharField(50) | '' | 地域/机房（如 cn-hangzhou）|
| `channel_mode` | CharField(10) | 'auto' | 执行通道模式：auto（按路由规则）/ direct（强制直连）/ agent（钉死）|
| `agent` | FK → agent_mgr.AgentNode | null | 钉死的执行通道 Agent（SET_NULL），仅 channel_mode='agent' 时生效 |
| `ssh_port` | PositiveIntegerField | 22 | SSH 端口 |
| `ssh_user` | CharField(50) | 'root' | SSH 用户 |
| `remark` | TextField | '' | 备注 |
| `created_at` | DateTimeField | auto | 创建时间 |
| `updated_at` | DateTimeField | auto | 最后更新时间 |

**status 枚举值：**

| 值 | 含义 |
|----|------|
| `running` | 运行中 |
| `stopped` | 已停止 |
| `maintenance` | 维护中 |

> 原 `env` / `department` 文本字段已由 `environment` / `project_group` 外键替代，
> 存量数据由迁移 `0004` 自动转换回填。

**默认排序：** `-created_at`（最新创建的优先）

---

## AgentRoute（执行通道路由规则）

**App：** `agent_mgr` | **表名：** `agent_route`

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `id` | BigAutoField | PK | 主键 |
| `agent` | FK → AgentNode | — | 目标 Agent（CASCADE，删 agent 连带删规则）|
| `project_group` | FK → ProjectGroup | — | 项目组，必填（PROTECT）|
| `environment` | FK → Environment | null | 环境，留空 = 该项目组默认规则（PROTECT）|
| `enabled` | BooleanField | True | 停用后该规则不参与解析 |
| `remark` | CharField(200) | '' | 备注 |

**唯一约束（两条带条件）：**

| 约束 | 条件 | 作用 |
|------|------|------|
| `uniq_agent_route_pg_env` | `environment IS NOT NULL` | 同项目组同环境只能有一条 |
| `uniq_agent_route_pg_default` | `environment IS NULL` | 同项目组的默认规则只能有一条 |

> SQL 中 `NULL != NULL`，环境为空的默认规则不能靠 `unique_together` 兜住，
> 必须单独一条带条件约束。

只对 `Server.channel_mode='auto'` 的主机生效，解析优先级见
`docs/backend/modules/agent.md`。

---

## AuditLog（操作审计日志）

**App：** `audit` | **表名：** `audit_log`

由 `AuditLogMiddleware` 自动写入，不支持手动创建/修改/删除。

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `id` | BigAutoField | PK | 主键 |
| `user` | FK → User | null | 操作用户（未认证为 null）|
| `action` | CharField(20) | — | 操作动作（同 method）|
| `path` | CharField(500) | — | 请求路径 |
| `method` | CharField(10) | — | HTTP 方法 |
| `ip` | GenericIPAddressField | null | 客户端 IP |
| `status_code` | PositiveSmallIntegerField | 0 | HTTP 响应状态码 |
| `body` | TextField | '' | 请求体（截断至 2000 字符）|
| `created_at` | DateTimeField | auto | 记录时间 |

**记录条件：**
- HTTP 方法在 `POST / PUT / PATCH / DELETE` 中
- 请求路径不以 `/api/schema` 或 `/admin/jsi18n` 开头

**默认排序：** `-created_at`

---

## SystemConfig（系统配置）

**App：** `system_cfg` | **表名：** `system_config`

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `id` | BigAutoField | PK | 主键 |
| `key` | CharField(100) | — | 配置键，全局唯一 |
| `value` | TextField | — | 配置值 |
| `description` | CharField(255) | '' | 用途说明 |
| `updated_at` | DateTimeField | auto | 最后更新时间 |

**默认排序：** `key`（字母升序）

**使用场景示例：**

| key | value | 说明 |
|-----|-------|------|
| `MAX_UPLOAD_SIZE` | `104857600` | 最大上传字节数 |
| `ALERT_EMAIL` | `ops@company.com` | 告警收件邮箱 |
| `MAINTENANCE_MODE` | `false` | 维护模式开关 |

---

## ApiCredential（API 凭证）

**App：** `auth_mgr` | **表名：** `auth_api_credential`

机读接口（内部脚本 / Webhook）的免登录身份。校验链与设计取舍见
`docs/backend/modules/auth.md` 的「API 凭证」章节。

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `id` | BigAutoField | PK | 主键 |
| `name` | CharField(64) | — | 凭证名称，唯一 |
| `key_id` | CharField(32) | — | 公开标识 `ak_<16位hex>`，唯一 + 索引，用于定位记录 |
| `secret_hash` | CharField(64) | — | secret 的 sha256（`common/secrets.py`），明文不入库 |
| `scopes` | JSONField | `[]` | 授权作用域数组，取值见 `apikey.SCOPES` |
| `ip_unrestricted` | BooleanField | `False` | 不限制来源 IP；**留空白名单必须显式置 True** |
| `ip_whitelist` | TextField | '' | IP 或 CIDR，逗号/换行分隔 |
| `sig_mode` | CharField(16) | `none` | `none` / `hmac_sha256` / `token` |
| `sig_header` | CharField(64) | '' | 签名请求头，如 `X-Hub-Signature-256` |
| `sig_prefix` | CharField(16) | '' | 签名值前缀，如 `sha256=`；留空表示纯 hex |
| `signing_secret_enc` | TextField | '' | 签名密钥（Fernet 加密，需原值重算 HMAC）|
| `rate_limit` | CharField(16) | '' | DRF 速率语法，留空用 `API_KEY_DEFAULT_RATE` |
| `bound_user` | FK(User) PROTECT | — | 服务账号，凭证的行权与审计身份 |
| `enabled` | BooleanField | `True` | 停用比删除可逆，泄露时的第一动作 |
| `expires_at` | DateTimeField | null | 过期时间 |
| `last_used_at` / `last_used_ip` | DateTime / Char(64) | null / '' | 使用痕迹（按 `API_KEY_TOUCH_INTERVAL` 节流写库）|
| `created_by` | FK(User) SET_NULL | null | 创建人 |
| `remark` | CharField(200) | '' | 备注 |
| `created_at` / `updated_at` | DateTimeField | auto | 时间戳 |

**默认排序：** `name`

**外键策略：** `bound_user` 用 `PROTECT`——删用户前必须先处理引用它的凭证，
避免凭证悄悄失去行权身份；`created_by` 是弱关联，用 `SET_NULL`。

---

## Role（角色）

**App：** `auth_mgr` | **表名：** `auth_role`

模块权限矩阵 + 执行能力位。档位、模块清单与「为什么不做成 CAM 策略语言」见
`docs/backend/modules/auth.md` 的「角色与权限矩阵」。

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `id` | BigAutoField | PK | 主键 |
| `name` | CharField(50) | — | 角色名称，唯一 |
| `code` | CharField(30) | — | 角色标识，唯一（小写字母/数字/下划线）|
| `permissions` | JSONField | `{}` | `{模块: 权限档}`，缺失模块视为 `none`（fail-closed）|
| `can_execute` | BooleanField | `False` | 可发起任务流执行、可处理暂停 |
| `can_exec_prod` | BooleanField | `False` | 可对生产环境发起执行 |
| `is_builtin` | BooleanField | `False` | 内置角色：可改矩阵、不可删 |
| `enabled` | BooleanField | `True` | 停用后绑定用户回退 `is_staff` 语义 |
| `remark` | CharField(200) | '' | 说明 |
| `created_at` / `updated_at` | DateTimeField | auto | 时间戳 |

**默认排序：** `id`（内置角色按播种顺序在前）

**User 侧新增字段**

| 字段 | 类型 | 说明 |
|------|------|------|
| `role` | FK(Role) SET_NULL | 空 = 回退 `is_staff` 语义，存量账号行为不变 |
| `allowed_project_groups` / `allowed_environments` | M2M | 数据范围，空 = 不限。只在主机列表、任务执行、变量解析三处生效 |

矩阵是**枚举**（模块 × 四档），配不出通配符、配不出「过宽」——写错最多是某个模块
给多给少了一档，不会一行 `*` 把整个平台交出去。这是它比策略语言安全的根本原因。
