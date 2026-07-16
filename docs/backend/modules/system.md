# 系统配置模块（system_cfg）

**路由前缀：** `/api/system/`  
**App Label：** `system_cfg`

---

## 职责

- 以键值对形式存储系统全局配置
- 支持运行时动态修改，无需重启服务
- 提供完整的 CRUD API

---

## 文件结构

```
apps/system_cfg/
├── __init__.py
├── apps.py          # AppConfig
├── models.py        # SystemConfig 模型
├── serializers.py   # SystemConfigSerializer
├── views.py         # SystemConfigViewSet
└── urls.py          # Router 注册
```

---

## 模型

```python
class SystemConfig(models.Model):
    key         = CharField(max_length=100, unique=True)  # 全局唯一
    value       = TextField()
    description = CharField(max_length=255, blank=True)
    updated_at  = DateTimeField(auto_now=True)

    class Meta:
        ordering = ['key']   # 字母升序
```

---

## ViewSet

```python
class SystemConfigViewSet(viewsets.ModelViewSet):
    queryset           = SystemConfig.objects.all().order_by('key')
    serializer_class   = SystemConfigSerializer
    permission_classes = [IsAuthenticated]
```

---

## 接口速览

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/system/configs/` | 配置列表 |
| POST | `/api/system/configs/` | 创建配置项 |
| GET | `/api/system/configs/{id}/` | 配置详情 |
| PUT | `/api/system/configs/{id}/` | 全量更新 |
| PATCH | `/api/system/configs/{id}/` | 部分更新 |
| DELETE | `/api/system/configs/{id}/` | 删除配置项 |

---

## 使用场景示例

| key | value | 说明 |
|-----|-------|------|
| `ALERT_EMAIL` | `ops@company.com` | 告警收件邮箱 |
| `MAX_UPLOAD_SIZE` | `104857600` | 最大上传字节（100MB）|
| `MAINTENANCE_MODE` | `false` | 全站维护模式 |
| `LOGIN_RETRY_LIMIT` | `5` | 登录失败次数上限 |
| `jumpserver.url` | `http://192.168.1.10` | JumpServer 地址（服务器注册用） |
| `jumpserver.token` | `<API Token>` | JumpServer 个人 API Token |
