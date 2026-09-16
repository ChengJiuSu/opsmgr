# 飞书接入模块（im_mgr）

**路由前缀：** `/api/feishu/`
**App Label：** `im_mgr`

---

## 职责

飞书与 opsmanager 之间的配置面。执行链路本身不在这里——飞书只是任务执行的
**又一个触发入口与展示出口**，最终都落到 `apps.task_exec.trigger`（与页面执行、
API 凭证触发同一段校验与落库逻辑）。

本期（T1）范围：

- 飞书自建应用凭据（App ID / App Secret / Encrypt Key / Verification Token）
- 回调入口地址（带随机串）与轮换
- 回调来源 IP 白名单
- 允许交互的飞书会话（群 / 单聊）白名单

后续：回调验签与事件入口（T3）、飞书 SSO 登录（T4）、任务执行卡片（T8）、
进度与暂停确认卡片（T9/T10）。

命名用 `im_mgr` 而不是 `feishu_mgr`：将来接钉钉 / 企微时是同一块菜单下的另一张
表，不用再开一个 app。

---

## 文件结构

```
apps/im_mgr/
├── apps.py          # AppConfig
├── models.py        # FeishuApp（单例）/ FeishuChannel
├── crypto.py        # 回调解密与验签（公网入口的第一道门）
├── dedupe.py        # 一次性消费：nonce / event_id / 卡片 token 共用，fail-closed
├── access.py        # 飞书侧发起执行的准入判定（四道闸，唯一入口）
├── cards.py         # 消息卡片模板（纯函数，无副作用）
├── tasks.py         # 出站异步调度（thread/celery/sync，失败只记日志）
├── handlers.py      # 回调的业务路由（事件 / 卡片动作）
├── sso.py           # 飞书网页免登：state、换 token、身份落到平台 User
├── client.py        # 出站客户端：token 缓存 + 统一请求封装 + 消息接口
├── serializers.py   # 秘密 write_only + has_xxx 回显、启用前置校验
├── views.py         # 配置单例 APIView + 会话 ViewSet
├── urls.py          # 路由
└── tests.py
```

---

## 模型

### FeishuApp（单例，pk 恒为 1）

| 字段 | 说明 |
|------|------|
| `app_id` | 飞书自建应用 App ID |
| `app_secret` | 加密存储（`EncryptedTextField`），换 `tenant_access_token` 用 |
| `encrypt_key` | 加密存储，解密回调体并参与签名校验 |
| `verification_token` | 加密存储，比对事件来源 |
| `callback_slug` | 回调路径随机串（32 位 hex），默认随机生成 |
| `ip_unrestricted` / `ip_whitelist` | 回调来源 IP 策略，**留空不等于不限制** |
| `enabled` | 整体接入开关 |
| `sso_enabled` | 飞书登录开关（需 `enabled` 先开） |
| `auto_create_user` | 未绑定用户时是否自动建号，默认 **关** |

取用一律 `FeishuApp.load()`；`save()` 强制 pk=1，`delete()` 直接抛错
（停用请置 `enabled=False`）。

**为什么是单例**：一个企业只会建一个自建应用。做成列表会让「到底哪条生效」
变成运行时问题，而且要到回调验签失败时才暴露。

**为什么回调路径带随机串**：事件与卡片回调是平台新增的两条公网入口，路径只配
在飞书后台，等于一层共享密钥，扫描器打不到入口。轮换 = 换回调地址，所以给了
独立动作而不是让 PUT 顺手改掉。

**为什么 IP 白名单存库**：与 API 凭证同一套决策（后台 DB 为主 + 防火墙兜底）。
飞书出口 IP 段会变，改一处配置比改 nginx 再 reload 快；nginx 那层留作兜底。

**`save()` 的两处补偿**：直接 `FeishuApp(...).save()`（不走 `load()`）时，
`created_at` 在新实例上是 None 而 Django 走 UPDATE，会把创建时间抹成 NULL；
`callback_slug` 有随机默认值，不回填就等于**静默轮换了回调地址**，表现是飞书那边
突然全部 404 且没有任何操作记录指向它。两者都在 `save()` 里从库中回填。

### FeishuBinding

平台用户 ↔ 飞书身份。**不映射到平台 User 的话，RBAC、`can_exec_prod`、数据范围
与审计全部失效**——这是整个飞书接入的安全底座。

以 `union_id` 为准（同一开发者账号下稳定），`open_id` 一并存下来供卡片回调反查
（那里只有 open_id）。`source` 留痕绑定来源，出问题时要能分清「管理员手工绑的」
与「按邮箱自动匹配上的」——后者依赖通讯录邮箱与平台账号邮箱一致，是最容易绑错
人的一条路。

### FeishuConfirmToken

生产执行的一次性确认票据。机器触发没有「弹窗再确认」那一步，确认被前移到第二张
卡片上，票据把三件事钉死：

- **一次性**：`used_at` 条件更新消费，并发双击只有一次生效；
- **限本人**：`open_id` 必须与点按钮的人一致——群里的卡片谁都看得见，不限人的话
  「A 发起、B 确认」就绕过了 A 的权限判定；
- **短有效期**（`FEISHU_CONFIRM_TTL`，默认 60s）：防止卡片被翻旧账再点一次。

票据**不携带任何执行参数**：任务、主机范围都在消费时按库里的当前值重新解析，
卡片上的值只用于展示。`consume()` 先查后条件更新——只用一条 update 也能保证
一次性，但那样所有失败原因都塌成「无效或已过期」，出问题时没法判断是点晚了、
点错人了还是重复点。

### FeishuChannel

允许与平台交互的会话白名单。群里的卡片谁都看得见、按钮谁都点得到——**鉴权按点
按钮的人判**（后续 handlers），而「哪些会话能把执行入口摆出来」是另一道闸。
未登记的会话一律只读。

`allow_execute` 与 `notify_enabled` 分开：多数群只需要收发布播报，不需要也不该
能从群里直接推生产。

---

## 回调校验（crypto.py）

任何人都能直接 POST 那两条公网地址，路径随机串只是让扫描器打不到入口，**真正
的门是验签**。`verify_callback(app, timestamp, nonce, signature, body)` 一次做完，
顺序固定、任一不过即拒：

| 序 | 校验 | 说明 |
|----|------|------|
| 0 | 来源 IP | 在视图层（T3）用 `app.ip_allowed(auth_client_ip(request))`，取 IP 需要 request |
| 1 | 签名 | `sha256(timestamp + nonce + encrypt_key + 原始请求体)`，恒定时间比对 |
| 2 | 时间戳 | 偏差 > ±300s 拒，压缩重放窗口 |
| 3 | nonce | `cache.add` 原子判重，窗口内只接受一次，重放窗口压到 0 |
| 4 | 解密 | AES-256-CBC，key = `sha256(encrypt_key)`，IV 取密文前 16 字节 |
| 5 | Verification Token | 兼容 `header.token`（v2 事件）与顶层 `token`（url_verification / 旧版卡片回调） |

三处容易写错的点，已固化进实现与测试：

- **签的是原始请求体字节**。解析后再 `json.dumps` 会改键序与空格，签名必不匹配；
  视图层必须把 `request.body` 原样传进来。
- **前三道排在解密之前**。它们不需要做密码学运算，便宜且能挡掉绝大多数噪声，
  也避免拿重放请求去跑 AES。
- **nonce 判重 fail-closed**。缓存不可用时按「已重放」拒绝——放行的后果是可被
  重放触发生产执行，拒绝的后果只是飞书那边报个失败（与 `common/sudo.py` 同取舍）。

`CallbackError` 的消息只进日志，**不回给调用方**：回包必须恒定，否则错误差异
本身就是一份探测反馈。

---

## 回调入口（views.py）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/feishu/callback/<slug>/event/` | 事件订阅 |
| POST | `/api/feishu/callback/<slug>/card/` | 卡片按钮回调 |

`<slug>` 即 `FeishuApp.callback_slug`，路由正则限定 32 位十六进制——形状不对的
请求连视图都进不去。

**认证与权限刻意置空**（`authentication_classes = []` + `AllowAny`）：这条路走的
是签名校验，不是 JWT。不置空的话请求会被全局的 `MfaJWTAuthentication` 挡在门外，
与机器接口走自己的认证类是同一类处置。

**回包恒定**：无论哪一道没过，一律 `200` + 固定包体（event 回 `{"code":0}`，
card 回 `{}`），失败原因只进日志。回包有差异本身就是一份探测反馈——攻击者能据此
区分「路径不对」「IP 不对」「签名不对」，逐步逼近。**飞书后台地址验证失败时看
服务端日志，不看回包。**

唯一的例外是事件订阅地址验证（`type=url_verification`），飞书要求原样回显
`challenge`。

**幂等两层**：nonce 判重挡的是原样重放；飞书自己的**投递重试**换了 nonce 与签名，
靠 `header.event_id` 再判一次（窗口 1 小时）。两者都 fail-closed。

**回调不写审计**：`/api/feishu/callback/` 整段在 `audit/middleware.SKIP_PATHS` 里。
群里每条消息都是一次事件回调，落进审计表只有噪声（匿名 + 一坨密文）；真正要留痕的
「谁从飞书发起了执行」由 im_mgr 在识别出操作人后显式写审计（T8）。飞书**配置面**
接口不在此列，照常审计。

**限速**：`FeishuCallbackThrottle` 按 `auth_client_ip` 分桶（默认 `240/min`，
`FEISHU_CALLBACK_RATE` 可调）。与 nginx 的 `limit_req` 两层——nginx 那层管不到
「已经在 IP 白名单里、但开始刷请求」的情况。

---

## nginx（deploy/nginx）

```
conf/templates/ops_manager.conf.template   # limit_req_zone + 回调 location
conf/snippets/feishu_callback_ips.conf     # 来源 IP 白名单（⚠ 待填）
```

三处要点：

- 回调 location **必须排在 `~ ^/(api|admin)/` 之前**。nginx 的正则 location 按
  定义顺序匹配，写在后面就永远命中不到，IP 白名单与限速全部失效，且没有任何报错。
- 正则里的 `{32}` **必须放在引号内**，否则 nginx 把裸的 `{ }` 当配置块边界。
- `feishu_callback_ips.conf` **为空 = 拒绝所有回调**（后面紧跟 `deny all;`）。
  上线前必须填入飞书公布的回调出口 IP 段并 reload；调试期可临时 `allow all;`，
  应用层白名单与签名校验仍然生效。

---

## 出站客户端（client.py）

所有出站调用统一从这里走。

- **token 缓存**：`tenant_access_token` 有效期 2 小时，缓存到共享缓存层（生产
  Redis），提前 300 秒判过期。WSGI / celery 多进程共用一份。
- **token 失效自愈**：飞书返回 `99991661/99991663/99991664/99991668` 时清缓存、
  重取、重试**一次**。不自愈的表现是「接下来两小时飞书全挂」；重试超过一次没有
  意义——真的是 App Secret 被改了的话，快点报错更有用。
- **缓存不可用时 fail-open**：退化为每次现取。这里失败只多打几次接口，与 nonce
  判重那种「放行即可被重放」不同，不需要 fail-closed。
- **失败一律抛 `FeishuError`**：调用方只 catch 一种异常。异步发卡任务只记日志，
  绝不能把飞书抖动回写成任务执行结果。

| 函数 | 说明 |
|------|------|
| `get_tenant_token(app=None, force_refresh=False)` | 取 token（默认读缓存） |
| `call(method, path, *, payload, params, app)` | 调业务接口，返回 `data`，业务错误码抛异常 |
| `send_card(receive_id, card, receive_id_type='chat_id')` | 发交互卡片，返回 `message_id`（更新卡片要用，务必落库） |
| `update_card(message_id, card)` | 整体替换卡片内容（执行进度刷新） |
| `send_text(receive_id, text, ...)` | 纯文本兜底 |
| `get_user(open_id)` | 通讯录取 `union_id` / `email`，供 SSO 绑定 |

`content` 字段必须是 JSON **字符串**而不是对象，传对象飞书报参数非法。

### 相关配置（settings / .env）

| 配置项 | 默认 | 说明 |
|--------|------|------|
| `FEISHU_BASE_URL` | `https://open.feishu.cn` | Lark（海外）改 `open.larksuite.com` |
| `FEISHU_TRUST_ENV` | `True` | 是否吃环境代理。与 `common/jumpserver.py` 的 `trust_env=False` 相反——那边目标在内网 |
| `FEISHU_CONNECT_TIMEOUT` / `FEISHU_READ_TIMEOUT` | 5 / 10 | 卡片回调要 3 秒内回包，真正的发卡走异步任务 |

---

## 交互链路（handlers.py）

```
@bot 任意消息   →  任务选择卡（下拉，只列该用户在本会话可发起的任务）
选中一条任务    →  确认卡（范围 / 模块数 / 当前匹配主机数）
点「执行」      →  非生产：直接 trigger.launch
                   生产：再出一张二次确认卡（一次性票据，限本人，60s）
点「确认执行到生产」→ 消费票据 → trigger.launch
```

刻意**不做 `/task run xxx` 那种命令行**：要记任务名、易打错，也没地方塞二次确认；
卡片把「看清楚再点」做成了流程本身的一部分。

三条贯穿始终的规矩：

- **鉴权按点按钮的人判**，不是按发卡片的人。每个动作都先把 `open_id` 解析成平台
  User（`FeishuBinding`），解析不到直接拒。
- **卡片上的值只用于展示**。除任务 id 外的一切（主机范围、模块、权限、开关）都在
  动作发生时按库里的当前值重新解析——卡片可能是十分钟前发的，期间角色和
  `im_enabled` 都可能变。测试里专门压了这条。
- **3 秒内回包**。按钮动作全部在响应体里直接回新卡片
  （`{'card': {'type': 'raw', 'data': …}, 'toast': …}`），一次往返搞定，不发出站请求。

### 出站异步（tasks.py）

需要**主动发卡**的路径（事件回调）走 `tasks.dispatch_send_card`：回调线程里发一次
出站 HTTP，少则几百毫秒、多则撞上飞书抖动直接把回调拖超时，飞书随即重试，同一件
事就被处理两次。调度后端沿用 `TASK_EXEC_BACKEND`（thread / celery / sync），与
task_exec、cloud_mgr 同一套口径。**失败只记日志**——飞书发不出去是飞书的事，
绝不能影响任务执行本身。

### 审计

回调路径整段在 `audit/middleware.SKIP_PATHS` 里，但「谁从飞书发起了执行」由
`handlers.audit()` 手写一条补上，只在识别出操作人之后记，记的是动作本身
（任务、log_id、模块数、主机数、是否经过生产确认）而不是那坨密文。

审计里的 `path` **刻意不带 `callback_slug`**：审计的读权限比系统配置宽，把回调
地址的随机串写进去等于把那层共享密钥降级保管。

---

## 卡片模板（cards.py）

**纯函数**：入参是普通对象，出参是可直接 `json.dumps` 的 dict。不碰数据库、
不发请求——卡片是这套交互里最容易反复改的部分，保持可单测、改起来才不用提心吊胆。

| 函数 | 用途 |
|------|------|
| `task_picker(tasks)` | 任务下拉；列表为空时**不渲染下拉**，只给说明 |
| `task_detail(task, server_count, step_count)` | 确认卡；0 台主机时只给「重新选择」 |
| `production_confirm(task, …, token, ttl)` | 生产二次确认（红标题，写死环境名与主机数） |
| `progress(task, log)` | 执行进度，随 `steps_result` 刷新同一条消息 |
| `result(task, log, tail=…)` | 终态；失败附日志尾段与失败模块名 |
| `pause(task, log, pause_type, …)` | 暂停处理：continue/abort 或 retry/skip/abort |
| `handled(text, by=…)` | 动作处理后的定格卡，按钮换成结论 |
| `notice(title, content)` | 拒绝原因等通用提示 |

按钮回传值约定 `{'act': 动作, ...}`，动作常量在模块顶部，handlers 从这里 import。
**value 只放标识不放状态**：卡片可能被翻出来重点，状态必须以库里为准。

三条硬约束（都是会出事的地方）：

- **用户内容一律走 `esc()`**：任务名里一个 `*` 或 `` ` `` 就能把卡片的 markdown
  结构带歪。飞书的 lark_md 不支持反斜杠转义，所以用全角同形字符替换。
- **日志尾段要剥反引号**：贴进代码块的日志若自带 ```` ``` ````，围栏会提前闭合，
  后面的内容直接当 markdown 渲染出去。
- **模块列表要截断**（`MAX_STEPS_SHOWN`）：卡片有大小上限，50 个模块的任务流
  全渲染会超。测试里用「50 模块 + 超长名字 + 10KB 日志」压着这条线。

生产二次确认的文案**写死环境名与主机数**：确认的意义在于让人看清自己在对什么
下手，只写「确认执行吗」等于没确认。按钮上那个飞书原生 `confirm` 弹窗不承担
安全职责——它只是客户端提示，拦不住重放，真正的确认是带一次性 token 的第二张卡。

---

## 执行准入（access.py）

飞书那边所有「能不能跑」的判断都走这里，**不在 handlers/cards 里各写一段**。
四道闸叠加，任一不过即拒：

| 序 | 闸 | 说明 |
|----|----|------|
| 1 | `ExecutionTask.im_enabled` | 默认关。开放哪几条任务是一次有意识的选择，不能全挂群里 |
| 2 | `default_select_all` | 飞书里没法逐模块勾主机；关掉默认全选的任务本就是要求人工确认（与接口触发同口径） |
| 3 | 会话白名单 | `FeishuChannel.executable(chat_id)`：未登记 / 停用 / 只收播报的会话都不给执行入口 |
| 4 | 人的权限 | 直接调 `task_exec.trigger.permission_error`（能力位 → 生产环境 → 数据范围），**不另写一份** |

`im_enabled` 与 `default_select_all` 的依赖关系在 `ExecutionTaskSerializer.validate`
里也拦一道，页面上勾不出非法组合；`access.check` 仍然兜一次存量数据与直接改库的情况。

**鉴权按点按钮的人判，不是按发卡片的人**：群里的卡片谁都看得见、按钮谁都点得到。
调用方必须先把 `open_id` 解析成平台 User（`FeishuBinding`），拿不到就传 `None`，
`check()` 直接拒。

`available_tasks(user, chat_id)` 给卡片的任务下拉用，只做「列得出来」的收窄
（任务属性 + 数据范围）；真正发起前仍要过一次 `check()`。

`needs_production_confirm(task)` 决定要不要走生产二次确认卡片——机器触发没有
「弹窗再确认」这一步，把确认动作前移到卡片上（一次性 token、限发起人本人、
60 秒有效，T8）。

第四道为什么要复用而不是新写：飞书这条路最容易被当成「内部工具」而放松，
它恰恰是最该跟页面一样严的。`permission_error` 因此从 `task_exec/views.py`
移到了 `task_exec/trigger.py`——那里本来就是页面执行与接口触发的共用入口。

---

## 飞书登录（sso.py）

```
GET  /api/feishu/sso/status/      登录页据此决定是否渲染「飞书登录」（公开，只回布尔）
GET  /api/feishu/sso/authorize/   取授权跳转地址 + 一次性 state
POST /api/feishu/sso/callback/    {code, state} → {access, refresh}
GET  /api/feishu/sso/binding/     当前用户的绑定状态（个人页展示，只读）
```

`binding` **不提供自助解绑**：自动创建的账号没有可用密码，解绑后就再也进不来了。
解绑属于管理动作，放在用户管理里（T11）。`union_id` 只回显尾段——它是内部标识，
展示只为让人确认「绑的是我这个飞书号」。

**不需要为 SSO 额外开放公网**：授权码由用户浏览器带回，飞书服务器不访问回调
地址；换 token 是平台的**出站**请求。所以 `sso_redirect_uri` 是给浏览器用的前端
路由（默认 `/login/feishu`），与 `/api/feishu/callback/` 那两条入站入口无关。

身份落地顺序：

1. `union_id` → `open_id` 查 `FeishuBinding`；
2. 查不到则按**企业邮箱**唯一匹配平台账号（个人邮箱是用户自己填的，拿它匹配等于
   让用户自选绑定对象），匹配到多个一律放弃；
3. 仍无：`auto_create_user` 关（默认）→ 明确拒绝并提示找管理员；开 → 建账号，
   **不设可用密码、不给角色**（落到无角色分支：只读矩阵、无执行能力）。

停用账号与服务账号一律拒登。`state` 后端签发、缓存 5 分钟、用掉即删，防登录 CSRF；
缓存不可用时**拒绝登录**（fail-closed）——这是登录路径。回调按来源 IP 限速，与
密码登录同一套 `common.ratelimit` 参数。

`FEISHU_AUTHORIZE_PATH` 单独抽成配置项：飞书文档上这条地址换过名字（旧版是
`/open-apis/authen/v1/index`），真换了改一行就行。

### MFA 口径

飞书登录签发的令牌 `amr=['sso']`，被 `MfaJWTAuthentication` 视为**已过第二因子**
（`auth_mgr.services.AMR_STRONG`）。理由：飞书登录本身已经是「受信设备上的飞书
客户端 + 飞书自身的二次验证」，再叠一层本平台 TOTP 是重复劳动。

**但 sudo 窗口不吃这一条**：改凭据 / 用户 / 系统配置仍要当场证明是本人——绑了
TOTP 用 TOTP，没绑的回退密码。登录一次能用 60 分钟而 sudo 只有 10 分钟，两者防的
不是同一件事。由此推出一个刻意的结果：**自动创建的账号没有可用密码，做不了配置面
的写操作**，那类账号本就不该直接拿到配置权限。

本地密码登录始终保留——飞书挂了要能进平台。管理员账号建议保留密码 + TOTP。

---

## 权限

全部接口：`module_perm('system', min_level='write')` + `SudoRequired`。

**不新开权限模块**：飞书应用配置与 LDAP / SMTP 同一个风险面——含连接口令，且
直接影响登录信任链（改 App ID 指向另一个飞书应用 + 打开自动建号 = 任意人可登录），
按 `roles.py` 的拆分判据本就归 `system`。

**`min_level='write'` 而不是按方法推导**：读到的是 App ID、回调地址与 IP 白名单，
等于一份「怎么打这两条公网入口」的说明书，GET 也不能对所有人开。

---

## 接口速览

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/feishu/app/` | 读配置（秘密不回显，返回 `has_xxx` / `missing_fields` / 完整回调地址） |
| PUT | `/api/feishu/app/` | 保存配置（秘密留空 = 不修改） |
| POST | `/api/feishu/app/rotate-slug/` | 轮换回调标识，飞书后台需同步改 |
| GET/POST | `/api/feishu/channels/` | 会话白名单列表 / 新增 |
| PUT/PATCH/DELETE | `/api/feishu/channels/<id>/` | 会话白名单维护 |

会话列表筛选：`chat_type`、`enabled`、`allow_execute`（`true`/`false`）、`keyword`。

---

## 校验规则

| 规则 | 说明 |
|------|------|
| 秘密字段留空 | 视为「不修改」，不会被清空（同云账号 / 连接凭据口径） |
| `enabled=true` | App ID / App Secret / Encrypt Key / Verification Token 必须齐全 |
| `enabled=true` | IP 白名单非空，或显式勾选 `ip_unrestricted`，否则拒绝 |
| `ip_whitelist` | 逐条解析 IP / CIDR，非法直接报错（不静默跳过） |
| `sso_enabled=true` | 必须 `enabled=true` |
| 会话 `allow_execute=true` | 必须 `enabled=true`，否则是个配了不生效的开关 |

`encrypt_key` / `verification_token` 在飞书侧可选，这里按**必填**处理：方案要求
回调强制验签，缺了就只能裸收，而那条入口在公网上。

---

## 部署提醒

- 秘密只经配置页录入、加密落库，**不进仓库、不进 .env**。
- 回调地址（`event_url` / `card_url`）在配置页直接复制到飞书开放平台。
- **上线顺序有依赖**：飞书后台填回调地址时会立刻发一次地址验证，而验证请求同样
  要过全部校验。所以必须先在平台侧配齐四项凭据 + IP 白名单并 `enabled=true`、
  且 nginx 的 `feishu_callback_ips.conf` 已填好，再去飞书后台保存回调地址。
- 轮换 `callback_slug` 后旧地址立即失效，飞书后台必须同步更新。
