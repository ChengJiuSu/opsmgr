# 认证模块（auth_mgr）

**路由前缀：** `/api/auth/`  
**App Label：** `auth_mgr`

---

## 职责

- 自定义用户模型（扩展 `AbstractUser`）
- JWT 登录（颁发 Access + Refresh Token）+ 登录失败限速
- MFA（TOTP 二次认证）：绑定、两步登录、恢复码、强制策略
- Token 刷新与验证
- **角色与权限矩阵**：模块 × 权限档 + 执行能力位 + 数据范围（替代二档 `is_staff`）
- **API 凭证**：内部脚本 / Webhook 的机读接口访问控制（key + IP 白名单 + 作用域 + 签名）

---

## 文件结构

```
apps/auth_mgr/
├── __init__.py
├── apps.py          # AppConfig
├── models.py        # User 模型
├── models.py        # User / UserMFA / MFARecoveryCode / ApiCredential
├── serializers.py   # CustomTokenObtainPairSerializer / MFA 入参 / ApiCredential
├── services.py      # MFA 业务：绑定、激活、校验、恢复码、强制范围
├── tokens.py        # 挑战票据（token_type='mfa'）+ 令牌签发（amr 声明）
├── authentication.py # MfaJWTAuthentication：全局 MFA 强制检查
├── roles.py         # 模块清单 / 分组 / 每模块可选档 / 内置角色矩阵 / 权限快照
├── permissions.py   # module_perm / capability_required：基于角色矩阵的权限类
├── apikey.py        # 机读访问控制：认证 + IP 白名单 + 验签 + 作用域 + 限速
├── views.py         # LoginView（限速+两步）/ MFA 六个接口 / UserViewSet / ApiCredentialViewSet
├── management/commands/mfa_reset.py            # 服务器 shell 兜底重置
├── management/commands/create_api_credential.py # 命令行签发凭证
├── tests.py         # 限速 / 绑定 / 两步登录 / 强制策略 / 管理员重置 / API 凭证
└── urls.py          # 路由

common/ratelimit.py  # 失败计数限速（登录、MFA 校验、凭证校验共用）
common/request.py    # client_ip（审计展示）/ auth_client_ip（鉴权，可信代理链）/ IP 规则匹配
common/secrets.py    # 机读凭证的明文生成与 sha256 哈希口径（与 agent 令牌共用）
common/totp.py       # TOTP（RFC 6238，标准库实现，RFC 官方向量守着）
common/crypto.py     # Fernet 对称加密（TOTP 密钥、Webhook 签名密钥必须可还原）
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
| `system_cfg.SystemConfigViewSet` | 存着 `jenkins.api_token`、`mfa.enforce_scope` 等全站安全策略 |
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

## 角色与权限矩阵

原来只有二档：`is_staff` 真 / 假。这撑不住真实诉求——想让某人只管 CMDB 不能执行
任务、只能对测试环境执行不能碰生产、外包只读，一个都做不到。

### 为什么不做成 CAM 那种策略语言

评估过腾讯云访问管理那套（action 注册表 + 资源 ARN + 通配符 + 求值引擎 + 策略
模拟器）：完整形态约 35~45 人日，且长期成本更高——每新增一个接口从「写两行
`permission_classes`」变成「登记 action + 想清 resource + 更新预设策略」，排障从
读一行权限类变成读一堆 glob 加跑模拟器，而**写错通配符静默扩权没有任何报错**。

那套是为「一个平台服务成千上万互不信任的租户」设计的。这里是 10 人内部平台、
无多租户，上它等于用多租户的复杂度解决一个班的权限问题。

矩阵能表达的东西够用，而且矩阵本身可读——不需要模拟器才能回答「这个角色能干
什么」。模块矩阵是策略文档的退化形式：将来真要上 CAM 可以机械翻译过去，不白做。

### 权限档（`roles.LEVELS`）

| 档 | 含义 | 替代原来的 |
|----|------|-----------|
| `none` | 该模块完全不可见 | — |
| `read` | 只读 | `IsAdminOrReadOnly` 的读侧 |
| `read_create` | 可读、可新增，不可改删 | `IsAdminOrReadCreate` |
| `write` | 全量读写 | `IsAdmin` |

保留 `read_create` 而不是简化成三档：CMDB 现在就是「常规用户可查可增不可改删」，
砍掉这一档会造成行为回退。

HTTP 方法 → 所需档位：GET/HEAD/OPTIONS → `read`，POST → `read_create`，
PUT/PATCH/DELETE → `write`；未知方法当 `write`（fail-closed）。推出来的档位还会
**向上取整到该模块的可选档**（见下），所以 `agent` 上的 POST 直接要 `write`。

### 模块（`roles.MODULES`）

13 个，粒度是「一块菜单 + 一个风险面」，不是 150 个 action：

| 分组 | 模块 | 覆盖 |
|------|------|------|
| 资产与配置 | `cmdb` | 服务器台账、服务器分配与服务规划 |
| | `dict` | 项目组 / 渠道 / 环境 / 云厂商 / 服务类型五张字典 |
| 任务执行 | `exec` | 任务流定义、执行记录、执行日志与日志文件 |
| | `method` | 方法库（命令 / playbook / Jenkins 模板）|
| | `variable` | 执行变量（含敏感变量真值）|
| | `artifact` | 构建制品 |
| | `credential` | 连接凭据（SSH 密码与私钥）|
| 云控管理 | `cloud` | 域名解析、证书签发与部署 |
| | `cloud_account` | 云账号 AccessKey / Secret |
| 系统管理 | `agent` | Agent 与执行通道路由 |
| | `account` | 用户、角色与 API 凭证 |
| | `system` | 系统配置 |
| | `audit` | 操作审计 |

**为什么从 8 个拆到 13 个**：第一版按「一个业务 app」切，结果四处「给 A 就顺带
给了 B」，都是提权面，中间档因此形同虚设——授权只能按组里最危险的那一项来给：

| 原模块 | 混在一起的东西 | 拆成 |
|--------|----------------|------|
| `cloud` | 域名 / 证书（日常运维）+ 云账号 AK/SK（拿到即整个云资产）| `cloud` + `cloud_account` |
| `system` | 系统配置 + 用户 / 角色 / API 凭证（改角色 = 给自己加权）| `system` + `account` |
| `exec` | 任务流 + 方法库（模板里能写任意命令）+ 变量（含密码类真值）| `exec` + `method` + `variable` |
| `cmdb` | 服务器台账 + 五张字典（改环境 `is_prod` 可绕过 `can_exec_prod`；项目组 / 环境同时是数据范围的维度）| `cmdb` + `dict` |

判据是「没有一个模块的写权限能顺带拿到另一个模块的能力」，不是「越细越好」——
细到 action 级就回到 CAM 那条路上去了。

### 每个模块的可选档位（`roles.MODULE_LEVELS`）

不是每个模块四档都有意义，页面上只渲染可选的那几档：

| 模块 | 可选档 | 为什么收窄 |
|------|--------|-----------|
| `audit` | none / read | 审计表没有写接口 |
| `artifact` | none / read / write | 制品只能由 CI 经 API 凭证上报，页面上没有新增 |
| `method` | none / read / write | 新增一条方法 = 把任意命令带进平台，不比改方法轻 |
| `agent` | none / read / write | 建 Agent 会下发一次性 token，重置 token 能顶掉现网连接 |
| `cloud_account` | none / read / write | read 只够在域名/证书页选账号（AccessKey 打码、Secret 不回显）|
| `account` | none / write | 用户 / 角色 / API 凭证是身份与提权面 |
| `system` | none / write | 系统配置含 LDAP / SMTP 等连接口令，读即泄露 |

收窄同时定义了方法档位的取整规则（`roles.required_level(method, module)`）：
取不到更强的可选档就 fail-closed 要 `write`。配得出来但点了不生效的档比没有这个
开关更糟——它让人以为自己给出去的是一个更小的权限。

### 配角色时的模块依赖

页面上的筛选下拉常常跨模块取数，配角色时要一起给，否则页面能进但下拉是空的：

| 页面 | 除自身模块外还需要 |
|------|-------------------|
| 服务器 / 服务器分配 / 任务流 | `dict` read（项目组 / 环境 / 渠道等筛选项）|
| 服务器编辑的「执行通道」 | `agent` read（Agent 下拉）|
| 域名 / 证书 | `cloud_account` read（账号下拉）；证书部署还要 `cmdb` read + `credential` read |

前端对这些下拉逐个按模块判权限再取，缺哪个就少哪个下拉——不会因为一个 403 把
整页选项都带走。

### 独立能力位

按模块档位表达不了、又是最高频诉求的，单独立字段：

| 能力位 | 含义 |
|--------|------|
| `can_execute` | 可发起任务流执行、可处理暂停中的任务 |
| `can_exec_prod` | 可对生产环境（`Environment.is_prod`）发起执行 |

生产单独立一位而不是塞进矩阵：「能改任务定义」与「能往生产推」是两件事——前者
错了可以改回来，后者错了已经推上去了。

`confirm`（处理暂停）与 `execute` 走同一道校验：continue / retry / skip 都会让剩余
模块真的跑起来，只在 execute 上拦等于留了个后门。

### 内置角色

| code | 名称 | 定位 |
|------|------|------|
| `super` | 超级管理 | 全模块最高档 + 全能力位（等于原来的 `is_staff=True`；audit 的最高档是 read）|
| `ops_admin` | 运维管理 | 日常运维读写；云账号 / 连接凭据 / Agent 只读，用户、角色与系统配置不可见 |
| `ops_member` | 普通运维 | 资产与字典可查可增；任务可执行但不能改定义、不能改方法、**不能碰生产** |

`ops_admin` 正是二档模型缺的那一层。内置角色**可改矩阵、不可删**——删掉「超级管理」
会把所有人锁在门外；序列化器还额外禁止把 `super` 降档，一次手滑就能锁死所有人。

### 无角色的用户（存量兼容）

`role` 为空时**严格回退**到原来的 `is_staff` 语义，做到「谁的权限都没变」：

- staff → 全模块 write + 全能力位；
- 非 staff → 内置「普通运维」的**模块矩阵**（资产与字典可查可增、其余只读），
  但**不含执行能力**。

最后这条容易写错：内置「普通运维」角色本身是 `can_execute=True`，但**回退不能套用它**
——角色落地前发起执行与处理暂停都是管理员专属，回退时把执行能力给出去等于这条迁移
悄悄给所有存量普通账号开了执行权。要给执行能力就显式配一个角色。

真正的收紧由管理员在页面上逐个改角色完成，而不是一次迁移悄悄改掉所有人的权限。

### 数据范围（`User.allowed_project_groups` / `allowed_environments`）

与 API 凭证共用同一套实现（`common/dimscope.py`）——两份实现迟早分叉，分叉的表现是
「页面上看不到的主机，用 API key 却能读到」。空 = 不限。

**只在三处生效**，都是能造成实际影响的入口：

| 生效点 | 方式 |
|--------|------|
| 主机列表 | 收窄可见集（`ServerViewSet.get_queryset`）|
| 发起任务执行 / 处理暂停 | 校验任务维度，越界拒绝 |
| 变量解析 `resolved` | 校验请求维度，越界拒绝（变量里可能有密码）|

其余列表**不做**过滤。那是整个评估里成本与漏检风险最大的一块（漏一处就是越权可见），
10 人无多租户不需要；字段与 `narrow()` 都留好了，将来要收紧只需在对应
`get_queryset` 上接一行。

### 权限快照

`roles.permission_snapshot(user)` 进 JWT 声明（`perms`）与 `/api/auth/me/`，前端据此
决定菜单与按钮可见性。它**只是少让人点到必然 403 的按钮**——真正判定始终在后端，
快照过期最多多显示一个按钮，点下去照样被拒。

### 顺带修掉的一个漏配

系统配置接口原来是 `IsAuthenticated + SudoRequired`——**任何登录用户过了 sudo 就能改**
`jenkins.api_token` / `mfa.enforce_scope`。随矩阵一并收到
`system` 模块 write。

---

## API 凭证（机读接口访问控制）

给**内部脚本与 Webhook** 用的免登录通道。它们没有浏览器、不做 MFA、也不该拿一条能干
所有事的 JWT。

替代的是原来那把全局 `artifact.api_key`（已下线）：一把 key 被所有脚本共用，轮换要同时改所有
调用方，泄露了也无法定位是哪个脚本漏的，且没有来源限制。

### 四道门（apps/auth_mgr/apikey.py）

| 门 | 内容 | 不过时 |
|----|------|--------|
| Key | `ak_<16位hex>.<43位随机串>`，前半段 `key_id` 建索引定位记录，后半段只存 sha256 | 401 `api_key_invalid` |
| IP 白名单 | 按凭证维护 CIDR 列表，客户端 IP 走 `auth_client_ip` 解析 | 403 `api_key_ip_denied` |
| 作用域 | `scope_required('artifact:report')`，一把 key 只能调授权的接口 | 403 `api_key_scope_denied` |
| Webhook 签名 | HMAC-SHA256（签原始请求体）或共享 Token，凭证上配了就强制 | 403 `api_key_signature_invalid` |

**IP 白名单与验签放在认证类里，不放权限类。** 项目里几乎每个 ViewSet 都自己声明了
`permission_classes`，把来源校验放权限层，新增一个机读视图时漏挂一个类就等于全开；
认证类是「用这把 key 就必过」的位置，漏不掉。

**白名单留空 ≠ 不限制**：必须显式把 `ip_unrestricted` 置 True，否则认证直接拒。留空
放行会让「忘了填」与「故意不填」外观一致，一次手滑就产出一把全网可用的 key。

### 真实客户端 IP：`common.request.auth_client_ip`

`X-Forwarded-For` 是普通请求头，客户端想写什么写什么。所以取 IP 有**两个口径**，
不能混用：

- `client_ip` —— 取 XFF 首段，可被伪造，只用于审计展示与限速分桶；
- `auth_client_ip` —— 鉴权用。`REMOTE_ADDR` 不在 `settings.TRUSTED_PROXIES` 内就
  整条 XFF 丢掉；在可信代理内则从 XFF **最右往左**剥掉连续的可信代理，取第一个非
  可信地址（右端是代理追加的，客户端伪造不到）。

剥完只剩代理自身时返回代理地址——它几乎不会在业务白名单里，于是校验失败。这是刻意的
fail-closed：配置不对时拒绝，而不是放过去。

部署要点：nginx 必须 `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`
（追加，不是覆盖），且 `TRUSTED_PROXIES` 要包含每一层代理/SLB/WAF 的地址。

### 与网络层防火墙的分工

两层都要，管的不是同一件事，不可互相替代：

| 维度 | 防火墙 / nginx allow-deny / 安全组 | 后台 per-key 白名单 |
|------|-----------------------------------|--------------------|
| 粒度 | 端口/路径级，全部 key 共用一套 | 每把 key 一套 CIDR |
| 改动成本 | SSH / 云控台 / reload，无自助 | 页面改，即时生效 |
| 审计 | 无系统内记录 | 进 audit 表，谁改的可查 |
| Webhook 源 IP | 段大且会变，手工同步易漏 | 按 key 单独维护，互不影响 |
| 抗扫描 | 强，流量进不来应用 | 弱，已经打到 Django |

实践：内部脚本（出口 IP 固定）两层都配；公网 Webhook 只配 per-key 白名单，别在
防火墙上限制（否则每加一个 Webhook 就要动网络）。nginx 侧示例见
`deploy/nginx/conf/templates/ops_manager.conf.template` 里 `/api` 之后的注释块。

### 模型 ApiCredential（表 `auth_api_credential`）

| 字段 | 说明 |
|------|------|
| `name` / `key_id` / `secret_hash` | 名称唯一；`key_id` 公开可查，secret 只存 sha256 |
| `scopes` | JSON 数组，取值见 `apikey.SCOPES` |
| `ip_unrestricted` / `ip_whitelist` | 不限制开关 + CIDR 文本（逗号/换行分隔）|
| `sig_mode` / `sig_header` / `sig_prefix` / `signing_secret_enc` | 验签配置；密钥需原值重算 HMAC，故走 Fernet 加密而非哈希 |
| `rate_limit` | DRF 速率语法，留空用 `API_KEY_DEFAULT_RATE` |
| `allowed_project_groups` / `allowed_environments` | 数据范围（M2M，空 = 不限），见下节 |
| `bound_user` | 服务账号（`svc-<名称>`，`is_service=True`、密码不可用、非 staff）。返回真实 User 让审计按 FK 落调用方，既有权限类也原样生效。生命周期见下 |
| `enabled` / `expires_at` | 停用比删除更可逆，泄露时的第一动作 |
| `last_used_at` / `last_used_ip` | 使用痕迹，按 `API_KEY_TOUCH_INTERVAL` 节流写库 |

明文只在创建 / 轮换的响应里出现一次，任何后续接口都取不到。

### 服务账号的生命周期

凭证绑定的 `svc-<名称>` 账号带 `is_service=True` 标记（独立字段而不是靠 `svc-` 前缀
判断——前缀可能撞上真人用户名，而这个标记要用来决定「能不能登录」「列表要不要显示」，
判错的后果不对称）。

| 事件 | 行为 |
|------|------|
| 建凭证 | 取或建 `svc-<名称>`，密码置为不可用；复用已停用的同名账号时重新启用 |
| 删凭证 | **停用**（`is_active=False`），不删除 |
| 用户列表 | 默认不列，`?include_service=true` 才出现；按 id 的详情/停用/删除**不过滤**（否则管理员对服务账号完全无从下手，表现是 404）|
| 设密码 | 一律拒绝（`reset_password` 动作与 `UserSerializer` 两处都拦）|
| 配角色 | 一律拒绝（`UserSerializer.validate`）；`role=null` 清空仍放行 |

**为什么删凭证只停用不删账号**：审计表的 `user` 外键是 `SET_NULL`，删掉账号会把历史
机读调用的归属抹成空——「这次上报是哪把凭证干的」就再也查不到了，那比留一个停用账号
糟得多。停用后它既登不进来（密码本就不可用）也认不过凭证校验（认证类检查
`is_active`）。

**为什么不许设密码**：密码被置成不可用正是为了「登不进来」。设了密码就多出一个能从
登录页进来、且因为非 staff 还可能绕过 MFA 强制范围的账号。

**为什么不许配角色**：机读授权只看**作用域 + IP 白名单 + 凭证数据范围**，服务账号
身上的角色矩阵对机读调用不生效（`UserOrScope` 按认证方式分流，凭证认证只判作用域，
见下节）。配得出来但不生效的开关比没有这个开关更糟——它让人以为已经把这把 key 收窄
了。角色字段留着只作审计身份，要收窄机读权限就改凭证的作用域或数据范围。

**为什么不做「作用域 AND 服务账号矩阵」**（评估后否决）：

- 兜不住要兜的那种配错。矩阵比作用域**粗**：`artifact:report`（只上报）与
  `artifact:download` 同属 `artifact` 模块，要让上报过就得给 write，而 write 连删
  制品都能干。作用域给宽了，矩阵拦不下来；
- 会当场打断线上。服务账号 `role=None` 走无角色回退（非 staff → 内置「普通运维」
  矩阵，`artifact: read`、`exec: read`），AND 之后 `artifact:report` 与
  `task:trigger` 直接 403 —— 所有 CI 上报与 webhook 触发全挂，除非先给每把 key
  配角色；
- 「这把 key 能干什么」要读两个地方。授权语义一分散就得靠模拟器回答问题，正是
  当初否决 CAM 策略语言的那条理由。

要收紧机读权限，正确方向是把作用域切得更细或收数据范围，不是叠一层更粗的矩阵。

**前端同口径**（`frontend/src/views/system/Users.vue`）：编辑服务账号时不渲染密码与
角色控件（角色位置改成一句「去『API 凭证』页改作用域」的提示），列表里也不给「重置
密码」按钮 —— 后端一律 400 的操作不该在页面上点得出来。

### 已登记作用域

| scope | 说明 | 挂在哪 |
|-------|------|--------|
| `artifact:report` | 上报构建制品 | `POST /api/artifacts/report/` |
| `artifact:download` | 下载构建制品 | `GET /api/artifacts/{id}/download/` |
| `task:trigger` | 触发任务流执行 | `POST /api/exec/tasks/{id}/trigger/` |
| `asset:read` | 读取主机清单（只读）| `GET /api/assets/servers/` |
| `variable:read` | 读取执行变量（不含敏感值）| `GET /api/exec/variables/resolved/` |
| `variable:secret` | 读取敏感变量真值（**高敏**）| 同上，配合 `include_secrets=true` |

新增机读接口时先在 `apikey.SCOPES` 登记，`scope_required()` 会校验拼写（拼错启动即报错）。

`HIGH_RISK_SCOPES`（当前只有 `variable:secret`）的凭证**禁止**勾选「不限制来源
IP」，序列化器直接拦。它能拿到密码类内容，泄露后果与其余作用域不在一个量级，
这里不给「我知道风险」的余地——真要放开就拆一把只带普通作用域的 key。

### 数据范围（`allowed_project_groups` / `allowed_environments`）

作用域管「能调哪类接口」，数据范围管「能作用在哪些资源上」，两层正交。留空 = 不限。

**纯收窄**：配不出比作用域更宽的权限，这是它比「路径通配符授权」安全的根本原因
——下拉多选表达不出「过宽」。

两种执行方式，按接口语义选：

| 接口 | 方式 | 为什么 |
|------|------|--------|
| 主机清单 | **收窄可见集**（`ApiCredential.narrow`）| 调用方忘了带筛选参数时拿到的是「我的全部」而不是「全公司的全部」，既好用又更难出错 |
| 变量解析 / 制品上报 / 制品下载 | **拒绝越界维度**（`allows_dims`）| 维度是必填参数，调用方已明确说了要哪一组，静默换成别的组更糟 |

### 为什么权限类不能用「或」

凭证绑定的是真实服务账号，`request.user.is_authenticated` 为真。所以

```python
permission_classes = [IsAdminOrReadCreate | scope_required('asset:read')]  # 错
```

的左边（只读放行）会直接命中——任何凭证不带任何作用域也能读。必须按认证方式分流：

```python
permission_classes = [user_or_scope(IsAdminOrReadCreate, 'asset:read')]    # 对
```

`user_or_scope` 生成的权限类：凭证认证的请求判作用域（默认只放行安全方法），
其余交给原来的权限类。`read_only` 默认为 True——一个「读」作用域顺带拿到写权限
比漏配权限更糟，因为凭证认证同时绕过了 MFA 与 sudo。

### 暴破与限速

- 校验失败按 `key_id` 计数（`common/ratelimit.py`），窗口内达 `API_KEY_FAIL_LIMIT`
  即锁定，锁定期内即使 key 正确也返回 429；
- `ApiKeyThrottle` 按 `key_id` 分桶限速。key 泄露且泄露方就在白名单里（比如 CI 机
  被人登上去）时，只有限速能限制损害速率。

### 调用示例

```bash
# 上报制品
curl -H "X-API-KEY: ak_xxx.yyy" -F file=@app.tar.gz \
     -F project_group=proj -F channel=official -F environment=prod -F version=1.2.3 \
     https://ops.example.com/api/artifacts/report/

# 触发任务流（生产环境必须显式确认）
curl -H "X-API-KEY: ak_xxx.yyy" -H 'Content-Type: application/json' \
     -d '{"confirm_production": true}' \
     https://ops.example.com/api/exec/tasks/12/trigger/
```

命令行签发（上线当天没人登得进来时的通道）：

```bash
./venv/bin/python manage.py create_api_credential \
    --name ci-jenkins --scopes artifact:report --ip 10.0.1.0/24
```

### 存量迁移（已完成）

`artifact.api_key` 的废弃期兜底已删除：`apps/artifact_mgr/permissions.py`、视图上的
`| HasArtifactAPIKey` 与系统配置项都不再存在，`ArtifactReportView` 只认
`scope_required('artifact:report')`。

一个连带项：那把 key 同时兼着制品下载签名密钥，下线时把签名迁到了独立的
`ARTIFACT_SIGN_KEY`（回退 `SECRET_ENC_KEY` → `SECRET_KEY`），见
`docs/backend/modules/artifacts.md`。

`apps/artifact_mgr/tests.py` 留了一条回归断言：即使有人把 `artifact.api_key`
写回系统配置，带它上报仍然 401。

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
| GET | `/api/auth/roles/` | 角色列表（system write，不分页）|
| GET | `/api/auth/roles/meta/` | 模块清单（含每个模块的可选档）+ 分组 + 权限档定义（前端渲染矩阵的唯一数据源）|
| POST/PATCH/DELETE | `/api/auth/roles/` `…/{id}/` | 角色增改删（system write + sudo；内置不可删）|
| GET | `/api/auth/api-credentials/` | API 凭证列表（管理员，不分页）|
| GET | `/api/auth/api-credentials/scopes/` | 可授权作用域清单 |
| POST | `/api/auth/api-credentials/` | 新建凭证，响应 `data.api_key` 为明文（仅此一次）|
| PATCH/DELETE | `/api/auth/api-credentials/{id}/` | 改 / 删（sudo 二次验证）|
| POST | `/api/auth/api-credentials/{id}/rotate_key/` | 轮换 key，旧 key 立即失效 |
| POST | `/api/auth/api-credentials/{id}/toggle_enabled/` | 停用 / 启用 |
