# 认证模块（auth_mgr）

**路由前缀：** `/api/auth/`  
**App Label：** `auth_mgr`

---

## 职责

- 自定义用户模型（扩展 `AbstractUser`）
- JWT 登录（颁发 Access + Refresh Token）
- Token 刷新与验证

---

## 文件结构

```
apps/auth_mgr/
├── __init__.py
├── apps.py          # AppConfig
├── models.py        # User 模型
├── serializers.py   # CustomTokenObtainPairSerializer
├── views.py         # LoginView
└── urls.py          # 路由
```

---

## 模型

```python
class User(AbstractUser):
    phone      = CharField(max_length=20, blank=True)
    department = CharField(max_length=100, blank=True)
    is_ldap    = BooleanField(default=False)
```

> 使用自定义用户模型需在 `settings.py` 中配置：
> ```python
> AUTH_USER_MODEL = 'auth_mgr.User'
> ```
> 该配置必须在第一次 `migrate` 前设置，否则需要重置数据库。

---

## 序列化器

```python
class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token['username'] = user.username       # 注入用户名
        token['department'] = user.department   # 注入部门
        return token
```

登录后返回的 JWT Token Payload 包含：

```json
{
  "token_type": "access",
  "exp": 1780992477,
  "user_id": "1",
  "username": "admin",
  "department": ""
}
```

---

## 视图

```python
class LoginView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer
```

---

## JWT 配置

```python
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME':  timedelta(minutes=60),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'ROTATE_REFRESH_TOKENS':  True,   # 刷新时生成新 Refresh Token
    'BLACKLIST_AFTER_ROTATION': True, # 旧 Refresh Token 立即拉黑
    'AUTH_HEADER_TYPES': ('Bearer',),
}
```

---

## 接口速览

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/auth/login/` | 登录，返回 access + refresh |
| POST | `/api/auth/token/refresh/` | 用 refresh 换新 access |
| POST | `/api/auth/token/verify/` | 校验 token 是否有效 |
