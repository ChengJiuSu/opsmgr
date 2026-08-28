# Agent 管理模块（agent_mgr）

**路由前缀：** `/api/agents/`（管理 API）、`/ws/agent/`（agent WebSocket 接入）
**App Label：** `agent_mgr`
**总体设计：** `docs/ops-agent-design.md`（协议、agent 侧实现、部署）

---

## 职责

NAT 出口执行代理（ops-agent）的服务端：agent 主动外连 WebSocket 长连接，
一条连接复用心跳、指令下发、日志回传三类流量。runner 把命令/剧本模块中
路由到 agent 通道的主机组打包为**自包含执行单元**（AgentJob）下发，agent
本地调用 ansible 执行并流式回传日志。

执行单元有四类 `step_type`：`command` / `playbook`（agent 侧调 ansible）、
`jenkins`（agent 侧调内网 Jenkins REST，用于 manager 在公网而 Jenkins 在内网
的场景，见 `docs/backend/modules/artifacts.md`）与 `cert_deploy`（云控证书分发
到内网主机，剧本由 agent 本地生成，见 `docs/backend/modules/cloud.md`）。

| 概念 | 说明 |
|------|------|
| `AgentNode` | 一台 NAT 出口主机上的 agent；token 只存哈希，明文仅创建/重置时展示一次 |
| `AgentJob` | 一次下发单元（任务流某模块的某 agent 主机组）；payload + 日志缓冲 + 终态；与 task_exec 仅以 task_id/log_id 整型弱关联 |
| `AgentRoute` | 通道路由规则：项目组（必填）+ 环境（可空 = 该项目组默认）→ agent，对 `channel_mode='auto'` 的主机与无主机单元（Jenkins）生效 |
| 执行通道 | `asset_mgr.Server.channel_mode` 三态 + `agent` 外键；解析口径见下节，全项目唯一实现在 `channel.py` |

---

## 文件结构

```
apps/agent_mgr/
├── models.py      # AgentNode / AgentJob / AgentRoute / hash_token（sha256 口径）
├── channel.py     # 执行通道解析：主机三态 + 路由规则 → (agent, source)；
│                  #   resolve_service_agent：无主机单元（Jenkins）的四级解析
├── consumers.py   # AgentConsumer：鉴权、心跳落库、log.chunk 幂等追加、result 终态
├── dispatch.py    # run_via_agent：创建 job → channel layer 下发 → 轮询终态（同步阻塞）
├── routing.py     # /ws/agent/ 路由（ops_manager/asgi.py 挂载）
├── serializers.py # token 不回显；job 列表不含 output 全文
├── views.py       # AgentNodeViewSet（agent 模块：read 可查、write 可改）+ regen_token / jobs 动作
└── tests.py       # API / Consumer（WebsocketCommunicator）/ dispatch / runner 分组
```

---

## 执行通道解析（channel.py）

```
Server.channel_mode='direct'                → 直连          source=direct
Server.channel_mode='agent' 且 agent 有效    → 该 agent      source=host
Server.channel_mode='auto'（默认）：
    AgentRoute(项目组, 环境, enabled)          → 该 agent     source=rule
    AgentRoute(项目组, 环境为空, enabled)       → 该 agent     source=rule
    无命中                                    → 直连         source=direct
```

- `resolve_channels(servers)` 批量解析（≤2 次查询：规则表 + 钉死 agent 的
  `in_bulk`），`resolve_channel/resolve_agent` 为单主机糖。调用方：
  `task_exec.runner._split_by_agent`、方法模块拦截、`cloud_mgr.deploy`、
  `ServerSerializer` 的 `effective_*` 字段。
- 「`mode=agent` 但 `agent` 为空」（agent 被删 SET_NULL）按 `auto` 处理。
- 规则命中不看 `AgentNode.enabled`：**禁用 agent 不改路由**，只影响建连，
  下发时 dispatch fail fast。要回退直连请停用规则（`AgentRoute.enabled=False`），
  否则 NAT 主机静默走直连只会换来 SSH 超时。
- **无目标主机的执行单元**（Jenkins 构建）走 `resolve_service_agent(candidates,
  project_group_id, environment_id)`：`candidates` 为 `[(source, agent_name)…]`
  按优先级（模块参数 → 方法配置 → 系统配置 `jenkins.agent`），取第一个非空名
  查库，全空时回落同一张 `AgentRoute` 表，仍无命中 → 直连。名字写了查不到
  抛 `ChannelError`（调用方转模块失败文案），**不静默回退直连**。
- 规则唯一性：`(项目组, 环境)` 与 `(项目组, 环境为空)` 两条带条件唯一约束
  （SQL 中 `NULL != NULL`，默认规则必须单独约束）；序列化器把冲突转中文报错。
- 上线顺序：**先把管理服批量置 `direct`，再建规则**，否则自身有公网 IP 的
  管理服会被项目级规则接管，出现「agent 通过自己执行自己」，且 agent 一挂
  连管理服都碰不了。

## 在线判定与踢旧连接

- agent 30s 心跳（应用层消息 + 传输层 ping/pong），Consumer 每条上行消息
  刷新 `last_heartbeat` 并置 online；断线时仅当断开的是当前登记连接
  （`channel_name` 匹配）才置 offline。
- 同名 agent 重复连接：新连接顶替，旧连接收 `agent.kick` 关闭（码 4409）。
- 鉴权失败：accept 后立即 close(4401)（先 accept 才能带关闭码）。

## 下发与可靠回传

- `run_via_agent(agent, payload, timeout, capability=None, secret_fields=())`
  同步阻塞（与
  runner 每模块同步语义一致）：离线/禁用 fail fast 不落 job；下发后 1s 轮询
  AgentJob 至终态，超时（模块 timeout + 60s 余量）置 failed(rc=124) 并转发
  `task.cancel`。`capability`（如 `'jenkins'`）非空时校验 agent `register`
  上报的能力清单（落 `meta.capabilities`）：不支持则 fail fast 提示升级；
  老 agent 不上报该字段时不校验，由 agent 侧 `task.ack` 拒收兜底。
  `secret_fields` 列出「只下发、不落 `AgentJob.payload`」的键（证书分发的
  `files`）：落库前换占位串，agent 侧 spool 同样脱敏，见设计文档 3.4.2。
- 日志分片 `log.chunk` 携带严格递增 `chunk_seq`：Consumer 只追加大于当前
  游标的分片（重复/乱序重 ack 游标，agent 按游标补传），单 job 输出上限
  2MB 超限截断。`task.result` 幂等，重复上报只重 ack。
- runner 侧（task_exec/runner.py）：命令/剧本模块主机按 `resolve_channels`
  分组，直连组走原 `run_subprocess`，agent 组生成**无密钥 inventory**
  （密码/私钥不出通道，SSH 免密凭据驻留 agent 主机）+ 变量下发；混合通道时
  输出按 `[直连 · n 台]` / `[经 Agent xx · n 台]` 标头拼接，纯直连输出与旧
  行为完全一致；rc 取首个非零。
- **payload 变量字段（新旧双发）**：manager 侧变量已改为落文件 `-e @vars.yml`，
  payload 同时带三个字段，兼容未升级的存量 agent：

  | 字段 | 谁用 | 内容 |
  |------|------|------|
  | `vars_file` | 新 agent | 变量管理 + 制品变量的 YAML 文本，落盘后 `-e @vars.yml` |
  | `step_extra_vars` | 新 agent | 模块额外变量原文，在 `vars_file` **之后**再 `-e` |
  | `extra_vars` | 旧 agent | 旧格式值列表（合并 JSON → 模块额外变量），逐个 `-e` |

  新 agent 见到 `vars_file` 字段即忽略 `extra_vars`（两者内容等价，同时用会
  重复注入但结果相同）。旧 agent 不认新字段，照旧只读 `extra_vars`。
  **agent 升级完成后**才可移除 `extra_vars`——提前删会让旧 agent 静默丢变量。
- 下发前把 payload 的 inventory 与变量归档到执行记录目录
  `TASK_LOG_DIR/<task_id>/<log_id>.d/step<order>/agent-<agent_id>/`，
  供事后在 agent 主机上复现。
- 方法模块：`ansible_playbook` provider 仍不走 agent 通道（遇 agent 通道主机
  提前失败并提示）；`jenkins` provider **已支持**，按 `resolve_service_agent`
  解析后下发 `step_type='jenkins'` 单元，payload 只含 job 名/构建参数/
  allow_unstable/timeout，**不含任何 Jenkins 凭据**（凭据在 agent 本地
  config.yaml）。

## 接口

```
GET/POST/PUT/PATCH/DELETE /api/agents/   Agent CRUD（读要 agent read、写要 agent write + sudo；创建响应含一次性 token；
                                          有未终态 job 禁止删除，可先禁用）
POST /api/agents/{id}/regen_token/       重置令牌（旧 token 即刻失效，响应含一次性新 token）
GET  /api/agents/{id}/jobs/              最近 50 条任务单元（排障；不含 output 全文）
GET/POST/PUT/PATCH/DELETE /api/agent-routes/   通道路由规则 CRUD（读要 agent read、写要 agent write + sudo，不分页，
                                          支持 agent/project_group/environment 过滤）
GET  /api/agent-routes/preview/?project_group=&environment=
                                          影响面预览：该维度下 channel_mode=auto 的主机
                                          （强制直连与钉死 agent 的主机不受规则影响，不计入）
WS   /ws/agent/                          agent 接入（握手头 Authorization: Token <raw>）
```

`asset_mgr` 关联：

- 服务器批量编辑 `update.channel_mode` 切换三态（`agent` 模式须同时给
  `update.agent`；其余模式自动清空 agent，不留不生效的残留配置）；
- `ServerSerializer` 增加只读 `agent_name`（主机显式指定的目标）与
  `effective_agent` / `effective_agent_name` / `channel_source` /
  `channel_source_label`（解析后的实际通道，列表页展示看后者）；列表序列化
  经 ListSerializer 一次性批量解析，不退化成 N+1。
- **旧客户端兼容**：只传 `agent` 不传 `channel_mode` 视为 `channel_mode='agent'`
  （单条与批量两条路径都覆盖）。外部脚本与导入接口可能只发 `agent`，去掉这条
  会让选中的 Agent 被静默清空。

## 消息协议摘要

上行（agent → manager）：`register`（含 `capabilities`：支持的 step_type
清单）、`heartbeat`、`task.ack`、`log.chunk`、`task.result`；下行：
`register.ack`、`task.dispatch`（`step_type` = command / playbook /
cert_deploy / jenkins）、`task.cancel`、`log.ack`、`result.ack`、`error`。
字段明细见设计文档第 3.3/3.4 节。

## 部署

- Channels + Redis channel layer（复用 celery 同一 Redis）；`daphne` 在
  INSTALLED_APPS 首位，开发 runserver 即 ASGI 同端口可测 WebSocket。
- 生产：daphne 监听 127.0.0.1:8001 承载 `/ws/`，nginx 按路径分流
  （见 deploy/nginx.conf 与 deploy/README 说明），HTTP 仍走现有 WSGI。
