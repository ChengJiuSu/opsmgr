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
    SKIP_PATHS   = ('/api/schema', '/admin/jsi18n')
    WRITE_METHODS = ('POST', 'PUT', 'PATCH', 'DELETE')

    def __call__(self, request):
        response = self.get_response(request)      # 先放行请求

        if request.method in WRITE_METHODS \
           and not request.path.startswith(SKIP_PATHS):
            self._record(request, response)        # 响应后记录

        return response
```

**记录内容：**

| 字段 | 来源 |
|------|------|
| `user` | `request.user`（未认证则为 `null`）|
| `method` / `action` | `request.method` |
| `path` | `request.path` |
| `ip` | `X-Forwarded-For` → `REMOTE_ADDR` |
| `status_code` | `response.status_code` |
| `body` | `request.body`（UTF-8 解码，截断至 2000 字符）|

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
