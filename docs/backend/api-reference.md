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
| `page_size` | integer | 20 | 每页条数，最大 200 |

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
  "remark": "主站 Web 服务器"
}
```

**成功响应（201）：** 返回创建的服务器对象（含 `*_name` 只读展示字段）

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

项目组 / 渠道 / 环境 / 云厂商四个模块接口形态一致（管理员可写，普通用户只读）：

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

## 文档 & 管理

| 地址 | 说明 |
|------|------|
| `GET /api/docs/` | Swagger UI 在线文档 |
| `GET /api/schema/` | OpenAPI JSON Schema |
| `/admin/` | Django 管理后台 |
