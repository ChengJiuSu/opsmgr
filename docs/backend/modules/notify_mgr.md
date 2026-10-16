# 消息通知模块（notify_mgr）

**路由前缀：** `/api/notify/`
**App Label：** `notify_mgr`
**权限：** 独立模块 `module_perm('notify')`（可选档 none / read / write），写操作叠 `SudoRequired`

---

## 职责

- 把业务事件（目前是任务执行结果）按**订阅规则**发到飞书；
- 两种发送方式，按需二选一或都配：
  - **应用机器人**：用「飞书接入」的自建应用，发到登记过的会话（`im_mgr.FeishuChannel`）；
  - **群机器人**：飞书自定义机器人 Webhook，只需要群里的 token，不需要飞书应用；
- 事件注册表是对外的扩展点：其他任务流 / 功能接入通知只需注册事件 + emit，
  规则匹配、去重、发送、发送记录都不用动；
- 落发送记录，排查「为什么没收到」。

**依赖方向**：业务模块不知道通知中心的存在。task_exec 只发
`task_exec.signals.execution_updated`，由本模块在 `apps.ready()` 里订阅
（`sources/task_exec.py`）。通知发不出去时任务照跑。

---

## 文件结构

```
apps/notify_mgr/
├── apps.py            # ready()：接入各通知来源（订阅业务信号）
├── models.py          # WebhookRobot / NotifyRule / NotifyEvent / NotifyDelivery
├── events.py          # 事件注册表 + Message + emit()（对外扩展点）
├── dispatch.py        # 规则匹配 → 渲染 → 逐目标发送 → 落记录；测试发送
├── webhook.py         # 飞书自定义机器人客户端（签名、限流、SSRF 防护）
├── tasks.py           # 异步调度（沿用 TASK_EXEC_BACKEND）、过期记录清理
├── sources/
│   └── task_exec.py   # 任务执行 → 三种事件；卡片内容渲染
├── serializers.py
├── views.py
├── urls.py
└── management/commands/clean_notify_records.py
```

---

## 模型

| 模型 | 表 | 说明 |
|------|----|------|
| `WebhookRobot` | `notify_webhook_robot` | 群机器人。`hook_token`（Webhook 地址最后一段）与 `secret`（签名密钥）均 `EncryptedTextField` |
| `NotifyRule` | `notify_rule` | 订阅规则：`events`（事件码 JSON 列表）+ `project_groups` / `channels` / `environments`（M2M，空 = 不限）→ `target_type` + `feishu_chat` 或 `webhook_robot`；`mention_trigger` @ 发起人 |
| `NotifyEvent` | `notify_event` | 一次事件。`dedupe_key` 唯一 = 同一件事只通知一次；维度存 id 不存外键；`context` 存事件当时的信息（如终止原因） |
| `NotifyDelivery` | `notify_delivery` | 一个事件发给一个目标的结果。`(event, target_key)` 唯一：两条规则指向同一会话只发一次 |

外键策略：规则从属于发送目标（会话 / 机器人删除 → 规则 `CASCADE`）；发送记录对规则
`SET_NULL`，并快照 `rule_name` / `target_name`，删了规则仍看得出当时发给了谁。

---

## 任务执行事件

| 事件码 | 时机 | 去重键 |
|--------|------|--------|
| `task_exec.success` | 执行记录落 `success` | `task_exec.log:{id}:success` |
| `task_exec.failed` | 执行记录落 `failed`（含进入「失败待处理」，卡片注明截止时间） | `task_exec.log:{id}:failed:{finished_at}` |
| `task_exec.failure_closed` | 失败待处理被人工终止或超时收尾 —— **最终失败** | `task_exec.log:{id}:closed` |

- 失败去重键带结束时间：失败待处理重试后**同一条执行记录**会再次结束，再失败是
  新的失败，要再通知；重复广播的结束时间相同，被挡住。
- 失败后重试成功同样会通知（旧的 im_mgr 播报按 `log_id` 缓存去重 1 小时，会把它吞掉）。
- `failure_closed` 由 `trigger.finish_failure` 显式广播（`kind='failure_closed'`, `reason`）：
  收尾不改执行状态，只看状态分不出「刚失败」与「最终失败」。
- **任务开关** `ExecutionTask.notify_enabled`（默认 True）：关闭后三种事件都不发。
  只管播报，不影响飞书交互卡片刷新与暂停 / 失败待处理时私聊发起人。
- 跳过已有实时卡片的会话：从飞书发起（或私聊推过处理卡）的执行，那张卡自己会刷成终态。

卡片内容：任务名、逐模块状态、失败模块、待处理截止 / 收尾原因、范围（项目组 / 渠道 /
环境）+ 生产标记 + 主机数 + 耗时、触发方式与发起人、失败时日志尾段、执行详情链接。

---

## 接入新的事件来源

```python
# apps/notify_mgr/sources/<来源>.py
from .. import events

def build(event):                    # 发送时调用，现读库现渲染；返回 None = 不发
    return events.Message(title='…', elements=[...], template='red')

events.register('workflow.failed', '工作流执行失败', group='工作流', builder=build)

# 事情发生时（必须立刻返回、绝不抛异常）
events.emit('workflow.failed', source_type='workflow.run', source_id=run.id,
            dedupe_key=f'workflow.run:{run.id}:failed',
            dims={'project_group': ..., 'channel': ..., 'environment': ...})
```

然后在 `apps.py` 的 `ready()` 里连上来源的信号。前端事件清单读 `/api/notify/events/`，
新事件自动出现在规则表单里。

---

## 群机器人

- 只存 token，地址由 `FEISHU_BASE_URL + /open-apis/bot/v2/hook/<token>` 拼出（防 SSRF）。
  录入时接受整条 Webhook 地址或 token，其他域名 / 路径一律 400。
- 签名：`sign = base64(hmac_sha256(key=f"{timestamp}\n{secret}", msg=""))`。
- 限流：本地每机器人每秒 4 条；撞飞书限流码（9499 / 11232 / 11233）退避 1.5s 重试一次。
- 只能发、不能改、按钮不能回调：只发结果卡，「重试 / 跳过」仍走平台或应用机器人卡片。
- 机器人设了「自定义关键词」时，消息里要含该关键词（卡片标题固定含「任务」）。

---

## 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/notify/events/` | 可订阅事件清单 `[{code, label, group, default}]` |
| GET | `/api/notify/chats/` | 规则表单的应用机器人会话下拉（id / 名称 / 类型 / 启用，不含 chat_id）|
| GET/POST | `/api/notify/robots/` | 群机器人列表 / 新增（`?keyword` `?enabled`） |
| GET/PATCH/DELETE | `/api/notify/robots/{id}/` | 详情 / 修改 / 删除（连带删除发往它的规则） |
| POST | `/api/notify/robots/{id}/test/` | 发测试消息；失败 400 带飞书错误信息 |
| GET/POST | `/api/notify/rules/` | 规则列表 / 新增（`?keyword` `?enabled` `?target_type` `?webhook_robot` `?feishu_chat`） |
| GET/PUT/PATCH/DELETE | `/api/notify/rules/{id}/` | 规则详情 / 修改 / 删除 |
| POST | `/api/notify/rules/{id}/test/` | 按规则的发送方式发测试消息 |
| GET | `/api/notify/deliveries/` | 发送记录（只读；`?status` `?event` `?target_type` `?keyword`） |

### 校验规则

| 字段 | 规则 |
|------|------|
| `WebhookRobot.hook_token` | 新增必填；修改时留空 = 不修改；只写，回显 `masked_token` |
| `WebhookRobot.secret` | 留空 = 不修改；`clear_secret=true` 清除；回显 `has_secret` |
| `NotifyRule.events` | 非空，且每个码都已注册；自动去重 |
| `NotifyRule.target_type` | `feishu_chat` 必须选会话，`feishu_webhook` 必须选机器人；保存时清空另一种目标 |

---

## 权限

| 档 | 能做什么 |
|----|---------|
| `none` | 菜单不可见 |
| `read` | 查看规则、群机器人（token 打码）、发送记录 |
| `write` | 增删改规则与机器人、测试发送（叠 sudo）|

- 没有 `read_create`：新增一条规则 / 机器人就是把执行结果发到一个新群，POST 向上取整到 write。
- 从 `system` 拆出：配告警不该顺带拿到 LDAP / SMTP 口令。会话下拉因此有自己的
  `/api/notify/chats/`，不走要 system 档的 `/api/feishu/channels/`。
- 出厂矩阵：super / ops_admin = write，ops_member = none。存量角色由
  `auth_mgr.0011_grant_notify_module` 补齐：自定义角色跟随其 `system` 档（write → write，否则 none），
  已配过的不覆盖。

## 异步与保留

- 投递沿用 `TASK_EXEC_BACKEND`（sync / thread / celery）。celery 任务
  `notify_mgr.deliver` 走默认队列，**不进 `task_exec` 队列**（那边并发只有 2）。
- 单个目标失败只记 `NotifyDelivery.status=failed` + 原因，不重试、不影响其他目标与任务本身。
- `NOTIFY_RECORD_RETENTION_DAYS`（默认 30）：beat `notify_mgr.clean_records` 每天清理，
  也可 `manage.py clean_notify_records [--days N]`。

---

## 迁移说明

`notify_mgr.0002_migrate_channel_broadcast` 把 `FeishuChannel.notify_*` 搬成规则（每个开着
播报的会话一条「<会话名> 播报」规则）：`notify_scope='failed'` → 失败 + 最终失败，
`'all'` → 再加成功；项目组 / 环境单值 → 规则多选。随后 `im_mgr.0011` 删除这几列。
可回滚（多选回写时取第一个值）。
