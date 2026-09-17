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
├── notify.py        # 执行进度回显：订阅执行广播 → 节流 → 异步刷卡
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

### FeishuCardMessage

一次执行 ↔ 那张卡片消息的对应关系（OneToOne）。进度靠**改同一条消息**呈现，
不是不停发新消息——飞书不适合流式日志，刷屏的代价比看不到中间态高得多。要改就
得记住 `message_id`，飞书只认这个。

从飞书发起时绑的是「被按钮替换掉的那条消息」，所以群里始终是同一条消息在原地变化。

### FeishuChannel

允许与平台交互的会话白名单。群里的卡片谁都看得见、按钮谁都点得到——**鉴权按点
按钮的人判**（后续 handlers），而「哪些会话能把执行入口摆出来」是另一道闸。
未登记的会话一律只读。

`allow_execute` 与 `notify_enabled` 分开：多数群只需要收发布播报，不需要也不该
能从群里直接推生产。

**播报订阅**：`notify_scope` 默认 `failed`——成功的执行每天几十上百次，全播等于把群
变成噪声源，而噪声一多，真正该被看见的失败就被划过去了。`notify_project_group` /
`notify_environment` 按维度收窄（空 = 不限），与平台其余地方同一套「维度」口径：
一个群只关心自己那摊事，别的项目组的失败播过来只会被当噪声。

---

## 回调校验（crypto.py）

任何人都能直接 POST 那两条公网地址，路径随机串只是让扫描器打不到入口，**真正
的门是验签**。`verify_callback(app, timestamp, nonce, signature, body)` 一次做完，
顺序固定、任一不过即拒：

| 序 | 校验 | 说明 |
|----|------|------|
| 0 | 来源 IP | 在视图层（T3）用 `app.ip_allowed(auth_client_ip(request))`，取 IP 需要 request |
| 1 | 签名 | `sha256(timestamp + nonce + encrypt_key + 原始请求体)`，恒定时间比对 |
| 2 | 时间戳 | 偏差 > ±300s 拒，压缩重放窗口。**两条路的格式不一样**：事件订阅给 Unix 秒，卡片回调给 Go 的 `time.Time` 字符串（`2026-09-16 17:22:51.15 +0800 CST m=+526362.48`），`parse_timestamp` 两种都吃；签名仍用**原样字符串**参与计算，解析只影响这一道 |
| 3 | nonce | `cache.add` 原子判重，窗口内只接受一次，重放窗口压到 0 |
| 4 | 解密 | AES-256-CBC，key = `sha256(encrypt_key)`，IV 取密文前 16 字节 |
| 5 | Verification Token | 兼容 `header.token`（v2 事件）与顶层 `token`（url_verification / 旧版卡片回调） |

### 地址验证是例外（联调踩到的）

**飞书不给事件订阅的地址验证请求带签名头**——那一步它只想确认你能解密并回显
challenge。实现最初把签名放在最前面、对验证请求也强制要求，结果飞书后台永远显示
「Challenge code 没有返回」，而服务端日志里是 `缺少签名头 X-Lark-Signature`。

现在按**有没有签名头**分两条路：

- **有签名头**（正式事件推送）：五道一道不少，另加一条「必须是加密体」——带签名却
  明文，飞书不会这么发，当异常来源处理；
- **没有签名头**：只放行 `type=url_verification`，且仍要过解密与 Verification Token
  两道。伪造者没有 Encrypt Key 就构造不出可解密的体，而这条路径**不触发任何业务
  动作**，只回显一个 challenge，放行的面是封闭的。除此之外缺签名头一律拒——不能让
  「不带签名」变成绕开验签的通道。

验证请求在飞书侧**没配 Encrypt Key 时是明文**，这条路径也接受，好让地址先验证通过；
但正式事件推送仍然必须加密（否则在「有签名头」那条被拦），所以 Encrypt Key 该配还得配。

拒绝时的日志会带上**收到了哪些 `X-Lark-*` 头**：回包是恒定的，飞书后台只显示
「没返回 challenge」，「带了哪些头」往往直接指出是飞书侧没开加密，还是中间某一跳把
头丢了。

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
  **缓存键带 App ID**，且保存配置时 `drop_token_cache()`——换自建应用（个人版 →
  企业版）或只换 App Secret 时，不隔离/不清缓存都会继续用旧 token，而飞书报的是
  「The authorization code does not match the provided application credential」，
  错误指向授权码、实际问题在凭证，排查会绕很大一圈。
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
点机器人菜单    →  同上，但私聊回（菜单不属于任何会话）
选中一条任务    →  确认卡（范围 / 模块数 / 当前匹配主机数）
点「执行（全部 N 台）」→ 非生产：直接 trigger.launch
                        生产：再出一张二次确认卡（一次性票据，限本人，60s）
点「选择主机」  →  多选表单卡（卡片 2.0，默认全勾）→ 提交后同上
点「确认执行到生产」→ 消费票据 → trigger.launch
```

### 主机勾选

**默认路径一步没多**：不点「选择主机」就是全部执行，与页面的「默认全选」一致。
勾选是可选支路，只在需要时才多一步。

用**卡片 2.0 的表单容器**：1.0 的交互组件各自独立回调，点「执行」时拿不到之前选了
什么，只能在服务端暂存选择状态——多一份会过期、会串会话的状态，不如让表单一次
带回。默认选中只能用 `selected_values`（`initial_options` 会被飞书的严格校验直接
拒掉，多一个属性整张卡都发不出去）。

`selected_host_ids()` **刻意写得容错**：飞书在不同版本/组件上回传的形状不一致
（`['1','2']` / `'1,2'` / `[{'value':'1'}]`，`form_value` 本身也可能直接是列表）。
判错的后果是「明明勾了却按全量执行」，在生产上不可接受，而容错的成本只有十几行。

**列出来的是「真正会动到的主机」**，不是裸的三维度匹配集。
`task.matched_servers()` 只按项目组/渠道/环境匹配，而**服务类型是模块级的**
（`TaskStep.service_type`）——页面的口径是「在全局范围里勾主机，每个模块再按自己的
服务类型收窄」。照搬那个集合到卡片上会列出一批根本不会被任何模块执行的主机：
勾了不生效，还让人以为会在上面跑。

`access.selectable_servers(task)` 取**跟随全局范围的模块**按各自服务类型收窄后的
**并集**；`use_custom_scope=True` 的模块按自己的维度实时匹配、压根不吃勾选，不计入。
确认卡上的「当前匹配 N 台」、多选项、以及选中值的校验集**全用同一个函数**——
两处对不上的话，「当前匹配 3 台」点进去只有 2 台，比列多了更让人困惑。

选中的主机**按当前匹配集重新解析**（`_resolve_selected`），不信卡片给的值：卡片
可能是几分钟前发的，期间主机可能已下线、改了维度或取消了服务规划。有一台对不上
就整体拒绝并要求重选——与页面 `execute` 接口「主机 id 不在全局范围匹配集内，
请刷新」同口径。

生产路径上，选中的主机**随确认票据存服务端**（`FeishuConfirmToken.server_ids`）：
卡片上的值只用于展示，而这批 id 决定生产上到底动哪几台。消费票据时仍会再解析
一次。

主机超过 `MAX_HOSTS_SELECTABLE`（50）台时不给勾选入口，只给整体执行并提示去页面
操作——下拉塞几百个选项既卡片超限也没法用。只有 1 台时也不给（没得选）。

两个事件入口殊途同归：`im.message.receive_v1`（群里 @机器人 / 私聊发消息）与
`application.bot.menu_v6`（机器人自定义菜单）。菜单事件有两处坑：**payload 里没有
chat_id**（菜单不属于任何会话，只能按操作人 open_id 私聊回），**操作人还多嵌一层**
（`operator.operator_id` 而不是 `operator`）——照着卡片回调的路径取会拿到空值，
表现成「你的飞书账号还没有绑定平台用户」，指向完全错误的方向。

### 单聊授权

会话白名单管的是「哪些会话能摆出执行入口」，但菜单事件没有 chat_id 可登记，所以
单聊用 `FeishuApp.allow_p2p_execute` 这个总开关表达同一个授权意思（默认关）。
判定收在 `access.session_allowed(chat_id, is_p2p)`：单聊看开关、群看白名单。

**场景标记随按钮 value 一路带下去**（picker → 详情 → 生产确认）。卡片回调的
`context` 里**没有 `chat_type`**，而**单聊的 `chat_id` 同样是 `oc_` 开头**——飞书
没有 `p2p_` 这类前缀，从 id 上分不出群聊还是单聊。照前缀猜的后果是单聊卡片全被
当成未授权的群，报出一句群聊分支的文案，指向完全错误的方向。拿不到标记时退回
「没有 chat_id 即单聊」。

单聊天然一对一、只有绑定用户本人可见，群里那种「谁都能点」的风险不存在；但
「只许从运维群发起、不许私聊发起」是合理诉求，所以仍要管理员显式点头。

未授权的会话**回一张说明卡而不是空下拉**——空下拉只会让人以为「没有任务」。

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

### 群播报（notify.broadcast）

执行终态播报给订阅了的会话。**与卡片刷新互不依赖**：页面发起的执行没有卡片，
但它的失败同样该让值班的人看见——这正是播报存在的理由。

三条规矩：

- **只播一次**：终态可能被重复广播（状态推送与模块表写入各来一次），缓存标记挡住
  重复。缓存不可用时**宁可多播一条**——漏播的代价是没人知道生产挂了（与 nonce
  判重的 fail-closed 刻意相反）。
- **跳过已经有卡片的那个会话**：从飞书发起的执行，那张卡已经在群里刷成终态了，
  再播一条一模一样的是纯噪声。
- **默认只播失败**，成功要 `notify_scope='all'` 才播。

播报内容复用终态卡（`cards.result`）：失败附日志尾段与失败模块名，带「查看完整
日志」链接。播报没有按钮——它是通知，不是操作入口。

### 暂停处理（收益最大的一张卡）

在此之前，任务卡在 `paused` 必须有人盯着页面。现在两条路都能处理：

- **从飞书发起的执行**：那张卡就地刷成处理卡（`continue/abort` 或
  `retry/skip/abort`），不另发消息；
- **页面发起的执行**：转 `paused` 时私聊推一张处理卡给发起人，并把这条消息绑到
  本次执行上——后续进度与终态照常就地刷新，私聊里也是同一条消息在变。

页面发起的执行**平时不打扰**（没绑卡片就短路），只有「需要人处理」时才推送。
推送只推一次：失败重试后再次失败会重复广播 `paused`，缓存标记挡住重复，真正的
去重是绑定关系——绑上之后就走刷新那条路了。

失败待处理的卡片附日志尾段，等待确认的不附：等待确认不是失败，贴一段日志只会
让人以为出错了。

**不判会话白名单**（`access.check_pause`）：这张卡可能是私聊推过来的，那个会话
不可能在白名单里。白名单管的是「哪些会话能把执行入口摆出来」，而这里处理的是一次
**已经发起**的执行。但人的权限一道不能少——`continue/retry/skip` 都会让剩余模块
真的跑起来，只在发起那头拦等于留了个后门（页面的 confirm 接口也是同一道口径）。

动作校验复用 `task_exec.trigger.apply_pause_action`（从 `views.confirm` 抽出）：
动作与暂停类型是否匹配这道校验各写一份的话，早晚有一边让 `failed` 状态收到一个
`continue` 信号。状态判定用条件更新，两人同时点只有一次生效。

还多一道**旧卡片**校验：任务本体的 `paused` 只属于最近一次执行，拿上一次执行留下
的卡片去点等于对着另一次执行下指令。

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

## 进度回显（notify.py）

订阅 `task_exec.signals.execution_updated`（在 `live.publish_steps` /
`live.push_status` 处广播），把「该刷卡片了」异步丢出去。

**信号定义在 task_exec、订阅在 im_mgr**：依赖方向不能反——task_exec 不该知道
im_mgr 的存在，否则「装不装飞书」就成了执行链路的依赖，而飞书发不出去时任务必须
照跑。也**没有用 `post_save`**：模块表是用 queryset `.update()` 写的（避开整行
保存），post_save 根本不触发。

四条硬规矩，每条都对应一个会真出事的场景：

| 规矩 | 不这么做会怎样 |
|------|----------------|
| 绝不在执行线程里发 HTTP | 信号在 ansible 的执行线程里同步触发；在那儿发请求 = 飞书抖一下任务慢一拍，飞书挂了任务卡住 |
| 渲染在**发送时**做，只传 log_id | 异步之间没有顺序保证；带快照投递的话，早投晚到的进度帧会把终态卡片覆盖回「执行中」 |
| 终态不节流（`ALWAYS_PUSH`） | 中间态丢一帧下一帧就补上了；终态丢了永远停在「执行中」，而那是唯一有人回头看的状态 |
| 失败只记日志 | 页面上的执行记录才是权威来源，卡片只是通知 |

中间态按 `THROTTLE_SECONDS`（3s）节流：模块多的任务流状态变化很密，不节流会把
飞书接口打成限流，卡片反而更新不上。节流缓存不可用时 **fail-open**（照常刷）——
与 `dedupe` 的 fail-closed 刻意相反：这里丢掉的只是一次界面刷新，多刷几次的代价
远小于卡片停在半路。

没绑卡片的执行（页面发起的）在 `_schedule` 最早处短路，不查状态也不投任务。

终态卡片的日志尾段取 `log.output`——它就是完整日志的尾部预览，够判断成败原因；
「查看完整日志」链接用 `SITE_BASE_URL` 拼执行详情页。

---

## 卡片模板（cards.py）

**纯函数**：入参是普通对象，出参是可直接 `json.dumps` 的 dict。不碰数据库、
不发请求——卡片是这套交互里最容易反复改的部分，保持可单测、改起来才不用提心吊胆。

### 全部是卡片 2.0

**一条消息的卡片版本不能中途切换**：在 1.0 消息的回调里返回 2.0 卡片（反之亦然）
会被飞书拒掉，客户端上表现为「出错了，请稍后重试 code: 200673」或干脆点了没反应。
所以整套统一 2.0，没有混用的余地。

升 2.0 的直接动因是**主机勾选需要表单容器**：1.0 的交互组件各自独立回调，点「执行」
时拿不到之前选了什么，只能在服务端暂存选择状态——多一份会过期、会串会话的状态。

真机撞出来的 2.0 规则（每条都对应一次失败）：

| 规则 | 报错 |
|------|------|
| 骨架是 `schema` + `body.elements`，不是顶层 `elements` | — |
| 文本用 `markdown`，没有 `div` + `lark_md` 嵌套 | — |
| **没有 `note` 标签** | `cards of schema V2 no longer support this capability` |
| **没有 `action` 容器**，横排用 `column_set` | — |
| 回调值在 `behaviors[].value`，顶层 `value` 不再接受 | — |
| 多选默认值只认 `selected_values` | `unknown property: initial_options` |

飞书的卡片 JSON 是**严格校验且逐个报错**——多一个未知属性整张卡都发不出去，每次
只告诉你第一个问题。所以别为了「兼容」往里塞备选字段，塞了就是下一次报错。排错时
看 `ErrPath`：它精确到元素下标与标签名。

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

**绑定关系单独挂 `account`**（不是 `system`）：它决定「哪个飞书号能以哪个平台账号
登录与发起执行」，和用户、角色、API 凭证是同一个身份面——改这里等于改身份映射。
读也需要 write 档：绑定列表本身就是一份「谁能从飞书进来」的清单。

**绑定没有更新接口**：改绑 = 解绑再绑，两步各自留痕比一次静默改值清楚得多。

手工绑定时管理员手上通常只有邮箱或手机号，`lookup` 动作按它反查 union_id 与
open_id。查不到就明说「通讯录里没有」，**不返回半个 id**——拿半个 id 建出来的绑定
永远匹配不上回调里的身份，而那种错很难从现象倒推。

序列化器里 `user` 与 `union_id` 两个字段**关掉了 DRF 的字段级唯一校验器**：它跑在
`validate()` 之前，报出来的是「具有 平台用户 的 飞书绑定 已存在」——看不出是谁
占着，而管理员最需要知道的恰恰是这个。唯一性仍由数据库约束兜底。

---

## 接口速览

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/feishu/app/` | 读配置（秘密不回显，返回 `has_xxx` / `missing_fields` / 完整回调地址） |
| PUT | `/api/feishu/app/` | 保存配置（秘密留空 = 不修改） |
| POST | `/api/feishu/app/rotate-slug/` | 轮换回调标识，飞书后台需同步改 |
| GET/POST | `/api/feishu/channels/` | 会话白名单列表 / 新增 |
| PUT/PATCH/DELETE | `/api/feishu/channels/<id>/` | 会话白名单维护 |
| GET/POST | `/api/feishu/bindings/` | 绑定关系列表 / 手工绑定（**account** write + sudo） |
| DELETE | `/api/feishu/bindings/<id>/` | 解绑 |
| POST | `/api/feishu/bindings/lookup/` | 按邮箱/手机号在飞书通讯录反查 union_id / open_id |

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

## 自检命令

```bash
./venv/bin/python manage.py feishu_check                    # 配置 + 出站连通性 + 地址
./venv/bin/python manage.py feishu_check --email a@corp.com # 顺带验通讯录反查
./venv/bin/python manage.py feishu_check --chat-id oc_xxx   # 真发一张测试卡片
```

**在去飞书后台保存回调地址之前跑它**。那一步一保存就立刻触发地址验证，而验证失败
时回包是恒定的（安全要求），从飞书后台完全看不出原因——只能翻服务端日志。先用这条
命令把「平台侧准备好了没」一次问清楚。

它覆盖三件离线确认不了的事：两种 token 是否取得到（`tenant_access_token` 与
`app_access_token` 是两把不同的 token）、授权页路径是否还叫这个名字（飞书换过，
404 就说明要改 `FEISHU_AUTHORIZE_PATH`）、通讯录里的邮箱字段是否有值
（`enterprise_email` 为空则飞书登录无法按邮箱自动绑定）。

除 `--to` 发卡外全部只读。

### 拿 chat_id / open_id

这两个值飞书界面上都不显示，登记会话白名单与手工绑定时都要用：

| 要拿什么 | 命令 | 前提 |
|----------|------|------|
| 群 `chat_id` | `--chats` | 机器人已在群里（`im/v1/chats` 只返回群，**单聊拿不到**） |
| 自己人的 `open_id` | `--to <平台用户名>`（看「收件人」那行） | 该用户已绑定飞书 |
| 任意人的 `open_id` / `union_id` | `--email <企业邮箱>` | 有通讯录权限 |

单聊没有可用的 `chat_id`，所以个人订阅播报直接在白名单里填 `open_id`
（`FeishuChannel.receive_id_type` 按 `ou_` 前缀自动切收件人类型）；而「单聊能不能
发起执行」压根不走白名单——菜单事件里没有 chat_id，用 `allow_p2p_execute` 总开关。

---

## 部署提醒

- 秘密只经配置页录入、加密落库，**不进仓库、不进 .env**。
- 回调地址（`event_url` / `card_url`）在配置页直接复制到飞书开放平台。
- **上线顺序有依赖**：飞书后台填回调地址时会立刻发一次地址验证，而验证请求同样
  要过全部校验。所以必须先在平台侧配齐四项凭据 + IP 白名单并 `enabled=true`、
  且 nginx 的 `feishu_callback_ips.conf` 已填好，再去飞书后台保存回调地址。
- 轮换 `callback_slug` 后旧地址立即失效，飞书后台必须同步更新。
