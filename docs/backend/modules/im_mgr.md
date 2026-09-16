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
- nginx 侧的 IP 白名单与限速见 T3（`deploy/nginx`），与库内白名单是两层。
