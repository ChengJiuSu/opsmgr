# API 接口文档

> 所有接口（登录除外）需在请求头携带 JWT Token：
> ```
> Authorization: Bearer <access_token>
> ```

---

## 统一响应格式

### 成功响应

```json
{
  "code": 0,
  "msg": "success",
  "data": { ... }
}
```

### 分页列表响应

```json
{
  "code": 0,
  "data": {
    "total": 100,
    "page": 1,
    "page_size": 20,
    "results": [ ... ]
  }
}
```

### 错误响应

```json
{
  "code": <HTTP状态码>,
  "msg": "错误描述",
  "data": null
}
```

---

## 分页参数

所有列表接口均支持：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `page` | integer | 1 | 页码 |
| `page_size` | integer | 20 | 每页条数，最大 200；传 `0` 跳过分页一次性返回全量（导出/同步场景） |

---

## 认证模块 `/api/auth/`

### 用户登录

```
POST /api/auth/login/
```

**权限：** 无需认证

**请求体：**

```json
{
  "username": "admin",
  "password": "admin123"
}
```

**成功响应（200）：**

```json
{
  "access": "eyJ...",      // 访问令牌，有效期 60 分钟
  "refresh": "eyJ..."      // 刷新令牌，有效期 7 天
}
```

Token Payload 额外字段：`username`、`department`

---

### 刷新 Token

```
POST /api/auth/token/refresh/
```

**权限：** 无需认证

**请求体：**

```json
{
  "refresh": "eyJ..."
}
```

**成功响应（200）：**

```json
{
  "access": "eyJ..."       // 新的访问令牌
}
```

> 刷新后旧 Refresh Token 立即失效（`ROTATE_REFRESH_TOKENS = True`）

---

### 验证 Token

```
POST /api/auth/token/verify/
```

**请求体：**

```json
{
  "token": "eyJ..."
}
```

**成功响应（200）：** `{}`（空对象，说明 Token 有效）

---

## 资产管理（CMDB） `/api/assets/`

### 服务器列表

```
GET /api/assets/servers/
```

**过滤参数：**

| 参数 | 类型 | 说明 |
|------|------|------|
| `status` | string | `running` / `stopped` / `maintenance` |
| `env` | string | 环境编码（如 `prod`）或字典 id |
| `project_group` | string | 项目组编码或字典 id |
| `channel` | string | 渠道编码或字典 id |
| `cloud_provider` | string | 云厂商编码或字典 id |
| `keyword` | string | 按名称 / IP 模糊搜索 |
| `page` | integer | 页码 |
| `page_size` | integer | 每页条数 |

**成功响应（200）：**

```json
{
  "code": 0,
  "data": {
    "total": 5,
    "page": 1,
    "page_size": 20,
    "results": [
      {
        "id": 1,
        "name": "web-prod-01",
        "ip": "192.168.1.10",
        "os": "CentOS 7.9",
        "cpu": 8,
        "memory": 16384,
        "status": "running",
        "environment": 3,
        "environment_name": "生产",
        "environment_code": "prod",
        "environment_is_prod": true,
        "project_group": 1,
        "project_group_name": "仙侠项目组",
        "channel": 1,
        "channel_name": "官服",
        "cloud_provider": 1,
        "cloud_provider_name": "阿里云",
        "region": "cn-hangzhou",
        "ssh_port": 22,
        "ssh_user": "root",
        "remark": "",
        "created_at": "2026-06-09T15:00:00+08:00",
        "updated_at": "2026-06-09T15:00:00+08:00"
      }
    ]
  }
}
```

---

### 创建服务器

```
POST /api/assets/servers/
```

**请求体：**

```json
{
  "name": "web-prod-01",          // 必填
  "ip": "192.168.1.10",           // 必填
  "os": "CentOS 7.9",
  "cpu": 8,
  "memory": 16384,
  "status": "running",            // running / stopped / maintenance
  "environment": 3,               // 环境字典 id，可为 null
  "project_group": 1,             // 项目组字典 id，可为 null
  "channel": 1,                   // 渠道字典 id，可为 null
  "cloud_provider": 1,            // 云厂商字典 id，可为 null
  "region": "cn-hangzhou",
  "channel_mode": "auto",         // auto（按路由规则）/ direct（强制直连）/ agent（钉死）
  "agent": null,                  // Agent id，channel_mode='agent' 时必填
  "remark": "主站 Web 服务器"
}
```

**成功响应（201）：** 返回创建的服务器对象（含 `*_name` 只读展示字段，以及
解析后的执行通道 `effective_agent` / `effective_agent_name` / `channel_source`
（host / rule / direct）/ `channel_source_label`）

> `channel_mode` 非 `agent` 时后端会清空 `agent`，避免残留不生效的配置；
> 只传 `agent` 不传 `channel_mode` 视为 `channel_mode='agent'`（旧客户端兼容）。

---

### 服务器详情 / 更新 / 删除

```
GET    /api/assets/servers/{id}/
PUT    /api/assets/servers/{id}/    # 全量更新
PATCH  /api/assets/servers/{id}/    # 部分更新
DELETE /api/assets/servers/{id}/
```

---

### 服务器统计

```
GET /api/assets/servers/stats/
```

**成功响应（200）：** `total` / `by_status` / `by_env` / `by_project` 分组计数

---

### CMDB 基础数据（字典）

项目组 / 渠道 / 环境 / 云厂商四个模块接口形态一致（普通用户可查、可新增，修改与删除仅管理员）：

```
GET    /api/assets/{module}/          # 列表，支持 enabled=true|false、keyword 过滤
POST   /api/assets/{module}/          # 创建
GET    /api/assets/{module}/{id}/     # 详情
PUT    /api/assets/{module}/{id}/     # 更新
PATCH  /api/assets/{module}/{id}/     # 部分更新
DELETE /api/assets/{module}/{id}/     # 删除（仍被服务器引用时返回 400）
```

`{module}` 取值：`project-groups` / `channels` / `environments` / `cloud-providers`

**公共字段：** `name`（名称）、`code`（唯一编码，限字母数字中划线下划线）、`enabled`（启用）、`sort_order`（排序）、`remark`

**差异字段：**

| 模块 | 字段 | 说明 |
|------|------|------|
| `project-groups` | `owner` / `contact` | 负责人 / 联系方式 |
| `channels` | `platform` | `all` / `android` / `ios` |
| `environments` | `is_prod` | 是否生产环境（联动前端标红等） |
| `cloud-providers` | — | 仅公共字段 |

**删除保护示例（400）：**

```json
{ "code": 1, "msg": "「生产」仍被 3 台服务器引用，无法删除", "data": null }
```

---

## 配置管理 `/api/config/`

### 批量归属分配

```
POST /api/config/allocation/assign/    （仅管理员）
```

**请求体：**

```json
{
  "server_ids": [1, 2, 3],
  "assign": {
    "project_group": 2,        // 提供且非 null → 设置
    "environment": null,       // 提供且为 null → 清空
    "channel": 5,
    "cloud_provider": 1,
    "region": "cn-hangzhou"    // 文本字段，"" 表示清空
  }                            // 未提供的字段保持不动
}
```

分配的字典项必须存在且处于启用状态；`server_ids` 必须全部有效。

**成功响应（200）：** `{"code": 0, "data": {"updated": 3}}`

---

### 分配概览

```
GET /api/config/allocation/overview/
```

**成功响应（200）：** `total`、各维度 `unassigned` 计数、`by_project` / `by_env` / `by_channel` 分布

> 配套：服务器列表接口支持 `unassigned=project_group|environment|channel|cloud_provider|any`
> 过滤，用于筛选漏配主机。

---

## 审计日志 `/api/audit/`

> 审计日志为只读接口，由中间件自动写入，不支持创建/修改/删除。

### 审计日志列表

```
GET /api/audit/logs/
```

**过滤参数：**

| 参数 | 类型 | 说明 |
|------|------|------|
| `page` | integer | 页码 |
| `page_size` | integer | 每页条数 |

**成功响应（200）：**

```json
{
  "code": 0,
  "data": {
    "total": 120,
    "page": 1,
    "page_size": 20,
    "results": [
      {
        "id": 1,
        "username": "admin",
        "action": "POST",
        "path": "/api/assets/servers/",
        "method": "POST",
        "ip": "127.0.0.1",
        "status_code": 201,
        "created_at": "2026-06-09T15:10:00+08:00"
      }
    ]
  }
}
```

---

### 审计日志详情

```
GET /api/audit/logs/{id}/
```

**成功响应（200）：** 返回单条审计日志

---

## 系统配置 `/api/system/`

### 配置列表

```
GET /api/system/configs/
```

**成功响应（200）：**

```json
{
  "code": 0,
  "data": {
    "total": 3,
    "page": 1,
    "page_size": 20,
    "results": [
      {
        "id": 1,
        "key": "MAX_UPLOAD_SIZE",
        "value": "104857600",
        "description": "最大上传文件大小（字节）",
        "updated_at": "2026-06-09T15:00:00+08:00"
      }
    ]
  }
}
```

---

### 创建配置

```
POST /api/system/configs/
```

**请求体：**

```json
{
  "key": "MAX_UPLOAD_SIZE",     // 必填，全局唯一
  "value": "104857600",          // 必填
  "description": "最大上传文件大小（字节）"
}
```

**成功响应（201）：** 返回创建的配置对象

---

### 更新配置

```
PUT   /api/system/configs/{id}/
PATCH /api/system/configs/{id}/
```

---

### 删除配置

```
DELETE /api/system/configs/{id}/
```

**成功响应（204）：** 无响应体

---

## Agent 与执行通道 `/api/agents/`、`/api/agent-routes/`

均仅管理员可访问，列表不分页。完整语义见 `docs/backend/modules/agent.md`。

| 方法与地址 | 说明 |
|------|------|
| `GET/POST/PUT/PATCH/DELETE /api/agents/` | Agent 节点 CRUD；创建响应含一次性明文 token；有未终态任务单元时禁止删除 |
| `POST /api/agents/{id}/regen_token/` | 重置令牌，旧 token 立即失效，响应含一次性新 token |
| `GET /api/agents/{id}/jobs/` | 该 agent 最近 50 条任务单元（排障，不含 output 全文）|
| `GET/POST/PUT/PATCH/DELETE /api/agent-routes/` | 通道路由规则 CRUD，支持 `agent` / `project_group` / `environment` 过滤 |
| `GET /api/agent-routes/preview/?project_group=&environment=` | 影响面预览：该维度下 `channel_mode='auto'` 的主机（`count` + 最多 200 条 `hosts`）|

**路由规则请求体：**

```json
{
  "agent": 1,                 // 必填
  "project_group": 2,         // 必填
  "environment": 3,           // 可空，null = 该项目组默认规则
  "enabled": true,
  "remark": "上海 NAT 出口"
}
```

同项目组同环境（或同项目组默认规则）重复时返回 400 中文报错。

**批量切换执行通道：**

```json
POST /api/assets/servers/batch/update/
{"server_ids": [1, 2], "update": {"channel_mode": "agent", "agent": 1}}
```

`channel_mode='agent'` 时必须同时给 `agent`；其余模式自动清空 `agent`。

---

## API 凭证（机读接口访问控制） `/api/auth/api-credentials/`

管理接口仅管理员且写操作要求 sudo 二次验证，列表不分页。完整语义见
`docs/backend/modules/auth.md` 的「API 凭证」章节。

| 方法与地址 | 说明 |
|------|------|
| `GET /api/auth/api-credentials/` | 凭证列表；`?keyword=&enabled=true|false&scope=` |
| `GET /api/auth/api-credentials/scopes/` | 可授权作用域清单 `[{value,label}]` |
| `POST /api/auth/api-credentials/` | 新建；响应 `data.api_key` 为明文，**仅此一次** |
| `PATCH/DELETE /api/auth/api-credentials/{id}/` | 改 / 删 |
| `POST /api/auth/api-credentials/{id}/rotate_key/` | 轮换 key，旧 key 立即失效 |
| `POST /api/auth/api-credentials/{id}/toggle_enabled/` | 停用 / 启用（泄露时优先用停用，可逆）|

**请求体：**

```json
{
  "name": "ci-jenkins",
  "scopes": ["artifact:report"],
  "ip_unrestricted": false,
  "ip_whitelist": "10.0.1.0/24, 203.0.113.9",
  "sig_mode": "none",
  "sig_header": "",
  "sig_prefix": "",
  "signing_secret": "",
  "rate_limit": "60/min",
  "expires_at": null,
  "remark": "Jenkins 制品上报"
}
```

`ip_whitelist` 留空必须同时给 `ip_unrestricted: true`，否则 400。`sig_mode` 取
`none` / `hmac_sha256` / `token`，非 `none` 时 `sig_header` 与 `signing_secret` 必填。
`signing_secret` 只写不回显，读接口给 `has_signing_secret` 布尔。

**机读接口调用方式：**

```
X-API-KEY: ak_7f3c9d2b1e5a4068.<secret>
# 或 Authorization: ApiKey ak_7f3c9d2b1e5a4068.<secret>
```

失败响应带 `error_code`，便于脚本区分原因：

| error_code | 状态码 | 含义 |
|------------|--------|------|
| `api_key_invalid` | 401 | key 不存在 / secret 不对 / 已停用 |
| `api_key_expired` | 401 | 凭证已过期 |
| `api_key_ip_denied` | 403 | 来源 IP 不在白名单（消息里带实际观测到的 IP）|
| `api_key_signature_invalid` | 403 | Webhook 签名校验失败 |
| `api_key_scope_denied` | 403 | 凭证未被授权调用该接口 |
| —（`Throttled`）| 429 | 超限速，或校验失败次数过多被锁定 |

---

## 文档 & 管理

| 地址 | 说明 |
|------|------|
| `GET /api/docs/` | Swagger UI 在线文档 |
| `GET /api/schema/` | OpenAPI JSON Schema |
| `/admin/` | Django 管理后台 |
