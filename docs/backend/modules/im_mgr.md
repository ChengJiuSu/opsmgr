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
├── handlers.py      # 回调的业务路由（事件 / 卡片动作）
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
