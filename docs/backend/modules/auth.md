# 认证模块（auth_mgr）

**路由前缀：** `/api/auth/`  
**App Label：** `auth_mgr`

---

## 职责

- 自定义用户模型（扩展 `AbstractUser`）
- JWT 登录（颁发 Access + Refresh Token）+ 登录失败限速
- MFA（TOTP 二次认证）：绑定、两步登录、恢复码、强制策略
- Token 刷新与验证

---

## 文件结构

```
apps/auth_mgr/
├── __init__.py
├── apps.py          # AppConfig
├── models.py        # User 模型
├── models.py        # User / UserMFA / MFARecoveryCode
├── serializers.py   # CustomTokenObtainPairSerializer / MFA 入参
├── services.py      # MFA 业务：绑定、激活、校验、恢复码、强制范围
├── tokens.py        # 挑战票据（token_type='mfa'）+ 令牌签发（amr 声明）
├── authentication.py # MfaJWTAuthentication：全局 MFA 强制检查
├── views.py         # LoginView（限速+两步）/ MFA 六个接口 / UserViewSet
├── management/commands/mfa_reset.py  # 服务器 shell 兜底重置
├── tests.py         # 限速 / 绑定 / 两步登录 / 强制策略 / 管理员重置
└── urls.py          # 路由

common/ratelimit.py  # 失败计数限速（登录与 MFA 校验共用）
common/request.py    # client_ip：X-Forwarded-For 首段 → REMOTE_ADDR
common/totp.py       # TOTP（RFC 6238，标准库实现，RFC 官方向量守着）
common/crypto.py     # Fernet 对称加密（TOTP 密钥必须可还原，不能只存哈希）
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

## MFA（TOTP 二次认证）

### 数据模型

```
UserMFA (OneToOne User)
├── secret_enc     Fernet 加密的 TOTP 密钥（common/crypto.py）
├── enabled        activate 验过一个真实验证码才置 True
├── activated_at / last_used_at
└── last_counter   已用过的最大时间窗，防同码重放

MFARecoveryCode (FK User)
├── code_hash      sha256（码是平台生成的 12 位随机串，约 60 bit 熵，
│                  无字典攻击面；慢哈希无法按哈希查库，只能逐条比对）
└── used_at        用一个作废一个
```

密钥为什么加密而不是像密码那样只存哈希：每次校验都要用原值重算验证码，必须
可还原。而明文入库等于「库泄露即可自造任何人的验证码」，MFA 就白做了。

### 绑定（两步，视图 → services）

```
POST /api/auth/mfa/setup/     → secret + otpauth URI + 恢复码（仅此一次明文）
POST /api/auth/mfa/activate/  → 带一个正确验证码才启用，并换发带 otp 的新令牌
```

- **先 setup 后 activate**：合成一步的话，用户扫错码或手机时间不对就把自己锁
  在门外，只能找管理员；
- 二维码在前端渲染（`qrcode` npm），后端只给 `otpauth://` URI，不引图像库；
- 已启用的绑定不允许再 setup 覆盖——否则拿到别人已登录会话的人可以悄悄把
  MFA 换到自己的验证器上。换设备要先 disable。

### 两步登录

```
POST /api/auth/login/    {username, password}
  ├─ 未绑定           → {access, refresh}（amr=["pwd"]）
  │                     强制范围内则附 mfa_enrollment_required: true
  └─ 已绑定           → {mfa_required: true, mfa_token, expires_in}  ← 不发访问令牌

POST /api/auth/mfa/verify/  {mfa_token, code}
  └─ 通过 → {access, refresh}（amr=["pwd","otp"]）
```

- 挑战票据 `token_type='mfa'`，5 分钟：simplejwt 的 `JWTAuthentication` 只认
  `AUTH_TOKEN_CLASSES`（AccessToken，`token_type='access'`），所以这张票据即便
  被当 Bearer 用也拿不到任何接口，**不需要额外拦截逻辑**；
- 一次性：用过的 `jti` 进缓存黑名单；单票据验证码错 `MFA_MAX_ATTEMPTS` 次即
  作废，逼回密码环节；
- 防重放：校验只接受**严格大于** `last_counter` 的时间窗，同一个 30 秒窗口的
  码用过一次就不再认（换新票据也不行）；
- 校验失败同样计入登录限速的按用户/按 IP 桶，换票据重试一样受限；
- 时间容差 ±1 窗口（±30s）。**上线前确认服务器 NTP**：时钟漂移会造成大面积
  校验失败，且现象酷似「密钥不对」。

### 恢复码

10 个（`MFA_RECOVERY_CODES`），字符集去掉 `0/O/1/I/L` 以便手抄。验证器丢了时
在登录第二步直接输恢复码，用一个作废一个，核销靠 `update(..., used_at__isnull=True)`
的条件更新保证并发下只生效一次。剩 ≤2 个前端提示重新生成。

### 强制策略

系统配置 `mfa.enforce_scope`：`off`（默认）/ `staff` / `all`。**能力先上线、
开关后拉**，避免部署当天把所有人挡在门外。

强制检查放在**认证类** `MfaJWTAuthentication` 而不是权限类：项目里大量 ViewSet
自己声明了 `permission_classes = [IsAdmin]` 等，全局默认权限对它们不生效，放
权限层等于漏掉一半接口。判定只读 token 的 `amr` 声明（RFC 8176），不查库。

未完成绑定时返回 **403 + `error_code: mfa_enrollment_required`**（`common/response`
透出 DRF 异常的 `default_code`），前端据此跳绑定页。白名单放行 `mfa/*`、`me`、
`logout`、`change-password`、`token/*`、`login`——少一条就会出现「被要求绑定，
却连绑定接口都调不了」的死锁。

存量 token 没有 `amr` 声明，视为只过了密码：开启强制后这些会话会被挡，重新
登录即可——这正是开启强制应有的效果。

### 丢设备兜底

| 通道 | 谁能用 | 说明 |
|------|--------|------|
| `POST /api/auth/users/{id}/reset_mfa/` | 管理员 | 清绑定，用户下次登录重新绑。不构成越权：管理员本来就能 `reset_password`，清 MFA 后仍要密码才能登 |
| `manage.py mfa_reset <username> [--yes]` | 服务器 shell | **开启全员强制前必须先确认这条命令可用**：最后一个管理员也丢了验证器时，Web 上就没人能给他重置了。验证请用 `--check`（只读报告状态），别拿真账号跑真清除 |

解绑与重发恢复码要**密码 + 当前验证码**双确认：只要密码不够（钓鱼拿得到），
只要验证码也不够（捡到已解锁手机就行），两个都要才算本人。

### 敏感操作二次验证（sudo 窗口）

登录验过一次 MFA 后，access 能用 60 分钟、refresh 能用 7 天。对**持久扩大权限**
的配置类写操作，这个跨度太长：一条被偷走的 token 在窗口内足够把整套凭据换掉。
这些接口因此额外要求「刚刚证明过是本人」。

```
POST /api/auth/mfa/sudo/   {code} 或 {password}   → 开窗口（SUDO_TTL，默认 600s）
GET  /api/auth/mfa/sudo/                          → {active, ttl, mfa_enabled}
```

- 启用了 MFA 的账号**必须给 code**，不接受密码：密码在钓鱼场景下拿得到，验证器
  拿不到，而 sudo 的意义正是「刚刚证明过是本人」。未启用 MFA 的账号退回密码
  确认，否则 `enforce_scope=off` 期间这道门形同虚设。
- 窗口状态在缓存里，键绑**当前访问令牌的 jti**（`sudo:<user_id>:<jti>`）：同一
  个人的另一条会话、或被偷走的另一个 token，都拿不到这次授权。代价是静默刷新
  令牌后要再验一次——access 60 分钟、sudo 10 分钟，实际很少撞上。
- 改密码、解绑 MFA 后主动 `revoke`：身份要素变了，之前那次证明不再算数。
- 失败计入限速（`sudo_user` 桶，阈值复用 `LOGIN_FAIL_*`）。
- **窗口已开着时 POST 幂等返回成功**，不再重复校验：前端「满 6 位自动提交」加上
  用户再回车会打两次，第二次拿同一个码必然撞防重放——报错却其实已经放行，是最
  费解的一种表现（用户看到的是「提示验证码不正确，但删除成功了」）。前端也加了
  同码只提交一次的防抖，两侧都堵住。
- 防重放拒掉的码**单独给文案**：「该验证码已使用过，请等验证器换出下一个 6 位码」。
  登录刚消费掉当前 30 秒窗口的码，紧接着开 sudo 就会撞上；说成「不正确」会让人
  反复怀疑密钥或手机时钟。
- **fail-closed**：缓存不可用时视为无授权、拒绝敏感操作。与 `common/ratelimit`
  的 fail-open 刻意相反——限速挂了放行只是少一层防爆破，sudo 挂了放行等于这道
  门直接没了；而拒绝的后果仅限「改不了配置」，日常读写照常。

受控范围（`common/sudo.py` 的 `SudoRequired` 与既有权限类**并列**声明，
`permission_classes = [IsAdmin, SudoRequired]`，DRF 是 AND 语义）：

| ViewSet | 为什么 |
|---------|--------|
| `task_exec.CredentialViewSet` | SSH 密码/私钥，改了等于换掉全站执行身份 |
| `auth_mgr.UserViewSet` | 建号、改权限、重置他人密码与 MFA |
| `agent_mgr.AgentNodeViewSet` | 建 agent 发一次性 token，重置 token 顶掉现有连接 |
| `agent_mgr.AgentRouteViewSet` | 改路由 = 改命令下发到哪台机器执行 |
| `cloud_mgr.CloudAccountViewSet` | 云 AK/SK |
| `system_cfg.SystemConfigViewSet` | 存着 `jenkins.api_token`、`artifact.api_key`、`mfa.enforce_scope` |
| `asset_mgr.ServerViewSet`（仅 PUT/PATCH/DELETE）| `channel_mode`/`agent` 决定命令下发到哪台机器执行，与通道路由同性质。**新增不拦**——新加的主机不会改变已有主机的执行通道（`common.sudo.required_for()` 生成只管指定方法的权限类）|
| `asset_mgr` 批量改/删、导入 | 一次能动一批主机的执行通道 |

只读方法（`SAFE_METHODS`）一律放行——sudo 防的是持久权限扩大，不是看一眼。
刻意**不**纳入的：

- **任务流执行**：密集发布时会反复弹验证框，而单次执行不扩大权限；
- **CMDB 字典**（项目组/渠道/环境/服务类型/服务规划）：改的是分类归属，不直接
  扩权，而这些是日常维护动作。

窗口是全局的、不分资源：在 Agent 管理验过一次，10 分钟内改用户或系统配置也不会
再弹——「为什么这次没弹」是它最容易被误判成漏拦的地方，顶栏因此常驻一个剩余
时间标签（`docs/frontend/views/security-mfa.md`）。

未授权时返回 **403 + `error_code: sudo_required`**，前端拦截器弹验证框并
**重放原请求**（见 `docs/frontend/views/security-mfa.md`）。

### 审计

MFA 相关请求都是写操作，由审计中间件记录；`code` / `secret` / `recovery_code`
在敏感键名表里，入库前打码（见 `docs/backend/modules/audit.md`）。

### 部署要点

- **必须执行迁移** `auth_mgr.0002`（建 `auth_user_mfa` / `auth_mfa_recovery_code`）。
  `LoginView` 每次登录都查 `UserMFA` 判断是否走第二步，漏迁移的表现是全站登不
  进去（`no such table: auth_user_mfa`），不是「MFA 功能不可用」而已。
  部署脚本与 docker 入口会自动跑，手工 `git pull` 的路径要自己补。
- `.env` 配 `MFA_SECRET_KEY`（留空则与 `SECRET_KEY` 绑死，之后无法单独轮换）。
- 开强制前先确认 `manage.py mfa_reset <用户名>` 可用、服务器 NTP 正常。
- 完整顺序见 `deploy/README.md` 的「上线后配置」。

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
| POST | `/api/auth/mfa/verify/` | 登录第二步：挑战票据 + 验证码/恢复码 → 正式令牌（免认证）|
| GET | `/api/auth/mfa/status/` | 当前用户 MFA 状态（是否启用、剩余恢复码、是否被强制）|
| POST | `/api/auth/mfa/setup/` | 开始绑定，返回密钥 / otpauth URI / 恢复码（仅此一次）|
| POST | `/api/auth/mfa/activate/` | 用验证码启用，并换发带 `otp` 声明的令牌 |
| POST | `/api/auth/mfa/disable/` | 解绑（密码 + 当前验证码）|
| POST | `/api/auth/mfa/recovery-codes/` | 重发恢复码（密码 + 当前验证码，旧码作废）|
| POST | `/api/auth/users/{id}/reset_mfa/` | 管理员清除他人绑定（丢手机兜底）|
| GET/POST | `/api/auth/mfa/sudo/` | 敏感操作二次验证窗口：查状态 / 开窗口 |
