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

| 概念 | 说明 |
|------|------|
| `AgentNode` | 一台 NAT 出口主机上的 agent；token 只存哈希，明文仅创建/重置时展示一次 |
| `AgentJob` | 一次下发单元（任务流某模块的某 agent 主机组）；payload + 日志缓冲 + 终态；与 task_exec 仅以 task_id/log_id 整型弱关联 |
| 执行通道 | `asset_mgr.Server.agent` 可空外键：空 = 直连（现状），有值 = 经该 agent 执行；agent 删除时 SET_NULL 回退直连 |

---

## 文件结构

```
apps/agent_mgr/
├── models.py      # AgentNode / AgentJob / hash_token（sha256 口径）
├── consumers.py   # AgentConsumer：鉴权、心跳落库、log.chunk 幂等追加、result 终态
├── dispatch.py    # run_via_agent：创建 job → channel layer 下发 → 轮询终态（同步阻塞）
├── routing.py     # /ws/agent/ 路由（ops_manager/asgi.py 挂载）
├── serializers.py # token 不回显；job 列表不含 output 全文
├── views.py       # AgentNodeViewSet（仅管理员）+ regen_token / jobs 动作
└── tests.py       # API / Consumer（WebsocketCommunicator）/ dispatch / runner 分组
```

---

## 在线判定与踢旧连接

- agent 30s 心跳（应用层消息 + 传输层 ping/pong），Consumer 每条上行消息
  刷新 `last_heartbeat` 并置 online；断线时仅当断开的是当前登记连接
  （`channel_name` 匹配）才置 offline。
- 同名 agent 重复连接：新连接顶替，旧连接收 `agent.kick` 关闭（码 4409）。
- 鉴权失败：accept 后立即 close(4401)（先 accept 才能带关闭码）。

## 下发与可靠回传

- `run_via_agent(agent, payload, timeout)` 同步阻塞（与 runner 每模块同步
  语义一致）：离线/禁用 fail fast 不落 job；下发后 1s 轮询 AgentJob 至
  终态，超时（模块 timeout + 60s 余量）置 failed(rc=124) 并转发
  `task.cancel`。
- 日志分片 `log.chunk` 携带严格递增 `chunk_seq`：Consumer 只追加大于当前
  游标的分片（重复/乱序重 ack 游标，agent 按游标补传），单 job 输出上限
  2MB 超限截断。`task.result` 幂等，重复上报只重 ack。
- runner 侧（task_exec/runner.py）：命令/剧本模块主机按 `Server.agent`
  分组，直连组走原 `run_subprocess`，agent 组生成**无密钥 inventory**
  （密码/私钥不出通道，SSH 免密凭据驻留 agent 主机）+ extra_vars 值列表
  下发；混合通道时输出按 `[直连 · n 台]` / `[经 Agent xx · n 台]` 标头
  拼接，纯直连输出与旧行为完全一致；rc 取首个非零。
- v1 限制：方法模块不走 agent 通道（ansible_playbook provider 遇 agent
  通道主机提前失败并提示）；模块执行中 agent 日志不实时进任务输出（终态
  后整段并入，v1.1 计划流式合并）。

## 接口

```
GET/POST/PUT/PATCH/DELETE /api/agents/   Agent CRUD（仅管理员；创建响应含一次性 token；
                                          有未终态 job 禁止删除，可先禁用）
POST /api/agents/{id}/regen_token/       重置令牌（旧 token 即刻失效，响应含一次性新 token）
GET  /api/agents/{id}/jobs/              最近 50 条任务单元（排障；不含 output 全文）
WS   /ws/agent/                          agent 接入（握手头 Authorization: Token <raw>）
```

`asset_mgr` 关联：服务器批量编辑接口 `update.agent` 支持批量切换执行通道
（null = 回退直连）；ServerSerializer 增加只读 `agent_name`。

## 消息协议摘要

上行（agent → manager）：`register`、`heartbeat`、`task.ack`、`log.chunk`、
`task.result`；下行：`register.ack`、`task.dispatch`、`task.cancel`、
`log.ack`、`result.ack`、`error`。字段明细见设计文档第 3.3/3.4 节。

## 部署

- Channels + Redis channel layer（复用 celery 同一 Redis）；`daphne` 在
  INSTALLED_APPS 首位，开发 runserver 即 ASGI 同端口可测 WebSocket。
- 生产：daphne 监听 127.0.0.1:8001 承载 `/ws/`，nginx 按路径分流
  （见 deploy/nginx.conf 与 deploy/README 说明），HTTP 仍走现有 WSGI。
