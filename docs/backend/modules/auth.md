# 认证模块（auth_mgr）

**路由前缀：** `/api/auth/`  
**App Label：** `auth_mgr`

---

## 职责

- 自定义用户模型（扩展 `AbstractUser`）
- JWT 登录（颁发 Access + Refresh Token）+ 登录失败限速
- Token 刷新与验证

---

## 文件结构

```
apps/auth_mgr/
├── __init__.py
├── apps.py          # AppConfig
├── models.py        # User 模型
├── serializers.py   # CustomTokenObtainPairSerializer
├── views.py         # LoginView（含失败限速）/ MeView / ChangePassword / UserViewSet
├── tests.py         # 限速：阈值、锁定期、清零、IP 桶、缓存故障放行
└── urls.py          # 路由

common/ratelimit.py  # 失败计数限速（登录，后续 MFA 校验共用）
common/request.py    # client_ip：X-Forwarded-For 首段 → REMOTE_ADDR
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

## 登录失败限速

固定窗口计数 + 锁定，状态放缓存（`common/ratelimit.py`，键按标识哈希后拼，
不把用户名明文摊在 Redis 里）：

| 桶 | 默认阈值 | 说明 |
|----|---------|------|
| 用户名（小写） | `LOGIN_FAIL_LIMIT=10` / 15 分钟 | 防单账号爆破，主力 |
| 客户端 IP | `LOGIN_FAIL_IP_LIMIT=50` / 15 分钟 | 防换用户名喷洒；**刻意宽松**——办公网整层共用一个出口 IP，阈值定小了一个人反复输错就能把全组挡在门外 |

- 触发后锁 `LOGIN_FAIL_BLOCK=900` 秒，锁定期返回 **429** 且**不再校验密码**
  （密码正确也不放行，否则限速只是给攻击者多一次机会）；
- 登录成功清零两个桶的计数与锁定；
- 账号停用导致的 401 同样计入失败；
- **缓存不可用时放行并告警**（fail-open）：Redis 挂掉把所有人挡在控制台外
  比放宽限速更糟，运维平台正是故障时最需要能登进去的系统。限速是辅助手段，
  密码强度与后续的 MFA 才是主防线；
- 生产必须 `CACHE_BACKEND=redis`：LocMemCache 是进程内的，多进程下阈值等于
  被乘以进程数（见 `docs/backend/configuration.md`）。

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
| POST | `/api/auth/login/` | 登录，返回 access + refresh；失败超阈值返回 429 |
| POST | `/api/auth/token/refresh/` | 用 refresh 换新 access |
| POST | `/api/auth/token/verify/` | 校验 token 是否有效 |
