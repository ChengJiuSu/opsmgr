# 审计模块（audit）

**路由前缀：** `/api/audit/`  
**App Label：** `audit`

---

## 职责

- 自动拦截并记录所有写操作（`POST / PUT / PATCH / DELETE`）
- 提供只读 API 供前端查询审计历史

---

## 文件结构

```
apps/audit/
├── __init__.py
├── apps.py          # AppConfig
├── middleware.py    # AuditLogMiddleware（自动记录）
├── models.py        # AuditLog 模型
├── serializers.py   # AuditLogSerializer
├── views.py         # AuditLogViewSet（只读）
└── urls.py          # Router 注册
```

---

## 中间件工作原理

```python
class AuditLogMiddleware:
    SKIP_PATHS    = ('/api/schema', '/admin/jsi18n')
    WRITE_METHODS = ('POST', 'PUT', 'PATCH', 'DELETE')

    def __call__(self, request):
        if recordable:
            self._preload_body(request)     # 关键：视图之前先缓存请求体
        response = self.get_response(request)
        if recordable:
            self._record(request, response) # 脱敏后入库
        return response
```

**请求体必须在视图之前取**：DRF 解析器直接从原始流读，读完
`request._read_started=True`，中间件事后再取 `request.body` 只会抛
`RawPostDataException` 被吞掉。改造前实测 413 条审计里只有 9 条带 body，
`/api/auth/login/` 的 114 条全是空的——审计的「记录请求内容」形同虚设。
现在预读一次 `request.body` 把它缓存进 `request._body`（Django 之后用
`BytesIO(_body)` 交给 DRF，解析不受影响）。

**multipart 不读**：制品上报会传数百 MB 的构建包，读它等于把整个包塞进内存
换一行日志；且存下来的是二进制片段（改造前库里已有这样一条），对排障无用。
这类请求体记 `<multipart 请求体未记录>`。

**记录内容：**

| 字段 | 来源 |
|------|------|
| `user` | `request.user`（未认证则为 `null`）|
| `method` / `action` | `request.method` |
| `path` | `request.path` |
| `ip` | `common.request.client_ip`（`X-Forwarded-For` 首段 → `REMOTE_ADDR`）|
| `status_code` | `response.status_code` |
| `body` | 脱敏后的请求体，截断至 2000 字符 |

## 敏感字段脱敏

审计表是给人查阅的历史记录，读权限比秘密本身宽，明文落进去等于把秘密降级
保管（改造前 `/api/auth/users/` 的建号请求就把 `password` 明文存了）。

```python
SENSITIVE_KEYS = {
    'password', 'old_password', 'new_password', 'confirm_password', 'passwd',
    'become_password', 'private_key', 'secret', 'secret_key',
    'token', 'access', 'refresh', 'api_token', 'api_key', 'apikey',
    'code', 'otp', 'mfa_code', 'recovery_code', 'crumb',
}
```

- 按**键名**（小写）递归打码为 `***`，嵌套 dict/list 一并处理，其余字段保留
  （用户名、路径这类排障线索不能一起丢）；
- 非 JSON 体无法按键定位敏感值：`/api/auth/` 下整体丢弃（不留半截密码），
  其他路径保留截断原文；
- 新增涉密字段时**同步加进这张表**——MFA 的 `code`/`secret`/`recovery_code`
  已预置。

**中间件在 Middleware 栈中的位置：**

```python
MIDDLEWARE = [
    ...
    'django.contrib.auth.middleware.AuthenticationMiddleware',  # 先认证
    ...
    'apps.audit.middleware.AuditLogMiddleware',                 # 后审计
]
```

---

## 模型

```python
class AuditLog(models.Model):
    user        = ForeignKey(User, on_delete=SET_NULL, null=True)
    action      = CharField(max_length=20)
    path        = CharField(max_length=500)
    method      = CharField(max_length=10)
    ip          = GenericIPAddressField(null=True)
    status_code = PositiveSmallIntegerField(default=0)
    body        = TextField(blank=True)     # 不通过 API 暴露
    created_at  = DateTimeField(auto_now_add=True)
```

---

## ViewSet

```python
class AuditLogViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,       # 只有列表和详情，无写操作
):
    queryset = AuditLog.objects.select_related('user').all()
```

> 使用 `select_related('user')` 避免 N+1 查询问题。

---

## 接口速览

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/audit/logs/` | 日志列表（分页）|
| GET | `/api/audit/logs/{id}/` | 日志详情 |

---

## 注意事项

- `body` 字段存储在数据库中但不通过 API 返回（防止敏感信息泄露）
- IP 地址支持 `X-Forwarded-For` 多级代理场景，取第一个有效 IP
- 审计日志本身的写入操作**不会**触发自身的审计记录（`/api/audit/` 为 GET，写入是内部 ORM 调用）
