# ops-agent 设计文档

> 状态：v1 已实现（2026-07-27）；Jenkins 代触发已实现（2026-08-25，见 3.4 / 4.6）。manager 侧模块文档：`docs/backend/modules/agent.md`；
> agent 代码：独立仓库 `~/workspace/ops-agent`。
> 关联模块：`agent_mgr`（新增）、`task_exec`（任务执行）、`asset_mgr`（服务器）
>
> **实现与本设计的差异**：agent 执行器未用 ansible-runner，改为直接
> `asyncio.create_subprocess_exec` 调 ansible（argv 无 shell 拼接、进程组
> 超时强杀、逐行流式；extra_vars 为字符串列表逐个 `-e` 传入，规避
> ansible-runner cmdline 的引号处理问题），artifacts 由 agent 自行落
> `inventory.ini / stdout.log / rc`。方法（method）模块 v1 不走 agent 通道
> （ansible_playbook provider 遇 agent 通道主机 fail fast 提示）；模块执行中
> agent 日志不实时并入任务输出（终态后整段并入，v1.1 计划流式合并）。

---

## 1. 背景与目标

部分项目主机位于 NAT 网络内，仅有私网 IP，ops-manager 无法直接 SSH 到达。
需要在 NAT 出口主机部署一个轻量执行代理 **ops-agent**：

- 与 manager 保持长连接，持续心跳上报存活状态；
- 接收 manager 下发的执行指令（ansible 命令 / playbook），在本地调用 ansible
  对内网主机执行，并把执行日志实时回传 manager；
- agent 自身日志与 ansible 执行日志均在本地留存；
- 严格控制出口主机上的资源占用。

**部署模式结论**：单点 agent（每个 NAT 网络出口部署一台），不采用蓝鲸式
全量 agent。理由：ansible 本身 agentless，单点 agent 本质是「把 ansible
控制节点放进内网 + 受控通道」；全量 agent 的价值（P2P 分发、秒级指标、
免 SSH）当前不需要，而其生命周期管理成本全要承担。

**非目标（v1 不做）**：主机指标采集、文件 P2P 分发、多 agent 负载均衡、
agent 自动升级。

---

## 2. 总体架构

```
┌────────────────────────── ops-manager ──────────────────────────┐
│  Django (WSGI, 现有 API)          ASGI (Django Channels, 新增)   │
│  ┌───────────┐   dispatch   ┌──────────────────┐                │
│  │ task_exec  │ ──────────▶ │ AgentConsumer     │◀── Redis      │
│  │ runner     │ ◀────────── │ (WebSocket 服务端) │    channel    │
│  └───────────┘  日志/结果    └────────┬─────────┘    layer      │
└──────────────────────────────────────┼──────────────────────────┘
                                       │ wss:// （agent 主动外连）
                     NAT 边界 ─────────┼─────────
                                       │
┌────────────────────────── NAT 出口主机 ─────────────────────────┐
│  ops-agent（Python / asyncio）                                   │
│  ┌────────────┐  ┌────────────┐  ┌───────────────────────────┐  │
│  │ 连接管理     │  │ 任务执行器   │  │ 本地留存                    │  │
│  │ 心跳/重连    │  │ ansible-    │  │ agent.log（轮转）           │  │
│  │ 收发/补传    │  │ runner 子进程│  │ artifacts/<uuid>/（归档）   │  │
│  └────────────┘  └─────┬──────┘  │ spool/（未确认日志缓存）      │  │
│                        │ SSH     └───────────────────────────┘  │
└────────────────────────┼────────────────────────────────────────┘
                         ▼
              NAT 内网主机（私网 IP，免装 agent）
```

要点：

- **连接方向恒为 agent → manager**（NAT 决定），manager 永不主动连 agent；
- 一条 WebSocket 连接复用三类流量：心跳、指令下发、日志回传；
- agent 不监听任何端口，攻击面最小；
- 内网主机零改造，仍由 ansible SSH 触达。

---

## 3. 通信设计

### 3.1 连接与鉴权

| 项 | 设计 |
|------|------|
| 协议 | WebSocket over TLS（`wss://<manager>/ws/agent/`） |
| 方向 | agent 主动外连，manager 侧 Django Channels 接受 |
| 鉴权 | 连接头 `Authorization: Token <agent_token>`；token 由 manager 侧「Agent 管理」生成，库中仅存哈希 |
| 单连接 | 同一 agent 重复连接时，新连接踢掉旧连接（防脑裂） |
| 重连 | 指数退避：1s → 2s → 4s → … 封顶 60s，加 ±20% 抖动 |

### 3.2 心跳与在线判定

- 传输层：WebSocket ping/pong，间隔 30s；
- 应用层：agent 每 30s 发送 `heartbeat` 消息，携带版本号、运行中任务数、
  1 分钟负载、磁盘水位；
- manager 判定：`last_heartbeat` 超过 90s（3 个周期）标记 `offline`，
  记审计日志；恢复心跳自动回 `online`；
- agent 离线期间下发到该 agent 的任务：模块直接失败（进入现有失败处理
  retry/skip/abort 流程），不做离线排队（v1 简化，避免僵尸任务）。

### 3.3 消息协议

JSON 文本帧，统一信封：

```json
{"type": "<消息类型>", "seq": 123, "ts": 1753600000, "data": {...}}
```

| 类型 | 方向 | data 字段 | 说明 |
|------|------|-----------|------|
| `register` | agent → mgr | `agent_name, version, hostname, capabilities` | 连接后首条消息，manager 校验后回 `register.ack`；`capabilities` 为支持的 step_type 清单（`command`/`playbook`/`cert_deploy`/`jenkins`），落 `AgentNode.meta`，下发时对老 agent fail fast（未上报则不校验） |
| `heartbeat` | agent → mgr | `running_tasks, load1, disk_free_pct, version` | 30s 周期 |
| `task.dispatch` | mgr → agent | 见 3.4 | 下发一个「模块执行单元」 |
| `task.ack` | agent → mgr | `job_id, accepted, reason` | 收单确认；队列满/磁盘不足时 `accepted=false` |
| `log.chunk` | agent → mgr | `job_id, chunk_seq, lines[]` | 日志分片，按 `chunk_seq` 严格递增 |
| `log.ack` | mgr → agent | `job_id, chunk_seq` | 累计确认（ack N 表示 ≤N 全收） |
| `task.result` | agent → mgr | `job_id, rc, elapsed, host_results{}` | 终态汇总 |
| `result.ack` | mgr → agent | `job_id` | manager 落库成功后确认，agent 才清理 spool |
| `task.cancel` | mgr → agent | `job_id` | 取消执行（kill 进程组） |
| `error` | 双向 | `code, message, ref_seq` | 协议级错误 |

可靠性规则：

- **日志不丢**：`log.chunk` 未收到 `log.ack` 前保留在本地 spool；断线重连后
  从最后 ack 位置续传；manager 按 `chunk_seq` 去重（重复分片幂等丢弃）；
- **结果不丢**：`task.result` 未收到 `result.ack` 前每次重连都重发；
- manager 侧对同一 `job_id` 的落库操作全部幂等。

### 3.4 任务下发 payload（`task.dispatch`）

manager 侧完成全部业务解析（主机匹配、变量注入、凭据选择），agent 收到的是
**自包含执行单元**，不回查 manager API：

```json
{
  "job_id": "uuid",
  "task_id": 42, "log_id": 128, "step_order": 2,
  "step_type": "command | playbook",
  "inventory": "<INI 文本，runner._inventory_content 生成>",
  "command": "systemctl restart game",
  "playbook": "site.yml", "tags": "restart",
  "extra_vars": {"pkg_version": "1.2.3"},
  "forks": 10,
  "timeout": 300,
  "project_sync": true
}
```

- `step_type='jenkins'` 的单元不涉及主机，payload 形状另一套（见下）；
- `inventory` 直接下发文本，agent 落临时文件后传给 ansible，**复用 manager
  现有动态 inventory 口径**（主机名别名、`service_types` 变量、服务分组）；
- NAT 内主机 `conn_ip` 回退私网 IP 的现有逻辑正好适配：agent 在内网，
  私网 IP 可达；
- **SSH 凭据不下发**：私钥驻留 agent 主机（`~/.ssh` 或配置指定路径），
  manager 只下发 `ansible_user/ansible_port`（已在 inventory 内）。降低
  密钥在通道与 manager 库中暴露的面。

### 3.4.1 Jenkins 执行单元（`step_type='jenkins'`）

manager 部署在公网、Jenkins 在公司内网时，manager 直连不通，改由 agent 代
触发（2026-08-25 落地）：

```json
{
  "job_id": "uuid",
  "task_id": 42, "step_order": 2,
  "step_type": "jenkins",
  "job_name": "build-app",
  "params": {"BRANCH": "release"},
  "allow_unstable": false,
  "timeout": 1800
}
```

- **凭据不下发**：Jenkins url/user/api_token 在 agent 本地 `config.yaml` 的
  `jenkins:` 段维护，与 3.4 的 SSH 私钥同一原则（密钥不出通道、不入 manager
  库）。agent 未配置该段 → rc=126 并回传中文提示；
- agent 侧流程：`buildWithParameters`（无参用 `build`，403 取 crumb 重试）
  → 队列轮询拿 build 号 → `logText/progressiveText` 增量拉控制台日志（剥
  ConsoleNote/ANSI，逐行经 `log.chunk` 回传）→ `api/json` 取构建结果；
- rc：0 = SUCCESS（或 UNSTABLE 且 `allow_unstable`），1 = 其余结果/请求失败，
  124 等待超时，143 manager 取消，126 payload/配置错误；
- `task.cancel` 与等待超时：agent 停止跟踪并按 `abort_on_cancel`（默认 true）
  `POST <build_url>/stop` 中止 Jenkins 上的构建；
- 与 ansible 单元**分开计并发**（`max_concurrent_jenkins_jobs`，默认 4）：
  Jenkins 单元是纯 IO 等待，不该占用 ansible 的执行槽；
- HTTP 用标准库 urllib（不新增 agent 依赖），阻塞调用经 `asyncio.to_thread`
  出让事件循环；忽略环境 `http_proxy`（内网 Jenkins 走代理只会拿 502）。

**副产品**：经 agent 的 Jenkins 日志是实时分片回传的，比 manager 直连模式
（终态整段返回）更早可见。

**已知边界**：agent 重启时该单元按 rc=137 上报中断，但 Jenkins 上的构建不随
agent 进程消亡，仍会跑完（与 ansible 子进程不同）；v2 可用 spool 里的
build_url 重连续跟。

### 3.4.2 证书分发执行单元（`step_type='cert_deploy'`）

内网主机的证书分发（2026-08-28 落地，替代 v1 的「agent 通道主机直接拒绝」）：

```json
{
  "job_id": "uuid",
  "step_type": "cert_deploy",
  "inventory": "<INI 文本，无密钥版本>",
  "deploy_path": "/etc/nginx/ssl",
  "reload_cmd": "nginx -s reload",
  "files": [
    {"name": "web-cert.crt", "mode": "0644", "content": "-----BEGIN CERT..."},
    {"name": "web-cert.fullchain.pem", "mode": "0644", "content": "..."},
    {"name": "web-cert.key", "mode": "0600", "content": "-----BEGIN PRIVATE KEY..."}
  ],
  "forks": 1,
  "timeout": 300
}
```

- **剧本由 agent 本地生成**（`executor.build_cert_playbook`），与 manager 侧
  `cloud_mgr.deploy._build_playbook` 同口径：建目录 0750 → copy 每个文件
  （`backup: true`）→ 可选 reload。**不下发剧本内容**：agent 的 ansible 执行面
  仍只有「项目目录内既有剧本」与这三个固定动作，避免开出任意剧本执行口子；
- agent 侧校验（不假设上游一定对）：`deploy_path` 必须绝对路径且不含 `..`；
  `name` 只收纯文件名（`[\w.@*-]+`，挡 `../` 与子路径）；`mode` 须 `0[0-7]{3}`；
  单文件 ≤ 256 KiB、合计 ≤ 1 MiB。任一不过 → rc=126 并回传中文原因；
- **证书内容的落盘面**（这是 v1 拒绝经 agent 的原因，现在按面逐个封）：
  - manager 侧 `AgentJob.payload` **不存** `files`（`dispatch.run_via_agent`
    的 `secret_fields=('files',)` 换占位串，DB 里没有 EncryptedTextField 保护）；
  - agent 侧 spool 的 `payload.json` 同样脱敏（`executor.redact_payload`）；
    崩溃恢复只把未完成 job 标 rc=137 上报、从不重放执行，故不影响可靠性；
  - agent 侧证书明文只写 `tempfile.mkdtemp()`（0700）内的 0600 文件，
    inventory 与生成的剧本一并放在那里（**不进 artifacts**，artifacts 有留存期），
    执行结束（含异常/超时/取消）在 `finally` 里整目录删除；
  - ansible `copy` 传的是 `src` 文件路径而非 `content`，故 stdout 与回传日志
    里不出现证书内容。
- **仍在通道内的暴露面**：证书内容随 `task.dispatch` 过 channel layer（Redis）
  一次。Redis 须只监听本机/内网并设密码——与 manager 库里的加密私钥相比，这是
  本方案唯一新增的暴露点，接受该取舍（另一路「agent 用一次性签名令牌回拉」
  需要 manager 开一个吐私钥的对外端点，暴露面更差）。

### 3.5 Ansible 项目目录同步（playbook 模块）

agent 主机维护一份与 manager 相同的 Ansible 项目目录（git 克隆同一仓库）：

- `project_sync=true` 时 agent 执行前跑配置的同步命令（默认
  `git pull --ff-only`），失败则任务失败并注明「剧本同步失败」；
- v2 增强：payload 带期望 commit，agent 校验后 checkout，保证 manager 与
  agent 剧本版本严格一致。

---

## 4. Manager 侧设计

### 4.1 新增模型（建议放 `apps/agent_mgr`）

```
AgentNode
├── name            唯一标识（如 nat-gw-sh01）
├── token_hash      鉴权 token 哈希（明文仅创建时展示一次）
├── status          online / offline / disabled
├── last_heartbeat  最近心跳时间
├── version         agent 版本
├── meta            JSON（hostname、load、磁盘水位等心跳快照）
└── enabled         禁用后拒绝连接
```

### 4.2 执行通道解析（主机三态 + 路由规则）

> v1 初版只有 `Server.agent` 一个可空外键（有值即经该 agent）。逐台绑定
> 新增主机易漏配，且无法表达「忽略规则、强制直连」——项目管理服自身有公网
> IP，不该被项目级规则拖进 agent 通道。故改为三态 + 维度规则（2026-08-24）。

`asset_mgr.Server`：

| 字段 | 语义 |
|------|------|
| `channel_mode='auto'`（默认） | 按 `AgentRoute` 路由规则解析，无规则命中则直连 |
| `channel_mode='direct'` | 强制直连，忽略路由规则（管理服等自身可达的主机） |
| `channel_mode='agent'` | 钉死到 `agent` 外键指定的 Agent |

`agent_mgr.AgentRoute`：`project_group`（必填）+ `environment`（可空 =
该项目组默认）→ `agent`，带 `enabled` 开关。**刻意只做两维且项目组必填**：
两级回落无歧义，无需优先级字段与重叠检测；唯一性由两条带条件的
`UniqueConstraint` 保证（SQL 中 `NULL != NULL`，环境为空的默认规则必须单独
约束）。渠道维不做——渠道通常不改变主机的网络位置，开了只增加规则爆炸。

解析优先级（`apps/agent_mgr/channel.py`，全项目唯一口径）：

```
channel_mode='direct'              → 直连                     source=direct
channel_mode='agent' 且 agent 有效  → 该 agent                 source=host
channel_mode='auto'：
    AgentRoute(项目组, 环境, enabled)   → 该 agent             source=rule
    AgentRoute(项目组, 环境为空, enabled) → 该 agent            source=rule
    无命中                              → 直连                source=direct
```

两条边界决定：

- 「`mode=agent` 但 `agent` 为空」（agent 被删，FK SET_NULL）按 `auto` 处理，
  不留不可执行的僵尸配置；
- 规则命中的 agent **不看 `AgentNode.enabled`**：agent 禁用只影响能否建连，
  下发时由 dispatch fail fast 给出明确原因。要让主机回退直连请停用规则本身
  （`AgentRoute.enabled=False`）——NAT 主机静默改走直连只会换来 SSH 超时。

服务器表单/批量编辑提供三态选择，Agent 管理页「通道路由」tab 维护规则并提供
影响面预览。**上线顺序**：先把各项目管理服批量置 `direct`，再建规则；顺序
反了会出现「agent 通过自己执行自己」。

v2 候选：规则加优先级与备用 agent 列表（主 agent 离线自动降级），逐台绑定
无法承载这个演进。

### 4.3 runner 改造（`task_exec/runner.py`）

执行到某模块时，按主机 `agent` 字段把模块主机**分组**：

```
模块主机 ──┬── 直连组（agent=None） → 现有 run_subprocess 本地执行
           └── agent 组（按 agent 聚合）→ 生成组内 inventory → task.dispatch 下发
```

- 每组独立生成 inventory、独立执行（本地组与各 agent 组可并发），输出按
  「通道标头 + 组输出」拼接进同一模块结果；模块 rc 取各组最大 rc；
- 下发路径：runner → Redis channel layer → `AgentConsumer.send`；runner
  阻塞等待该 job 终态（复用现有模块内同步语义），超时 = 模块 timeout +
  下发余量（如 +30s）；
- agent 离线 / `task.ack` 拒收 / 等待超时 → 该组 rc=1，走现有失败处理
  （retry 会重新下发新 `job_id`）；
- `task.cancel`：现有中止流程触发时向 agent 转发取消。

### 4.4 日志接入

- `log.chunk` 到达 → 追加进当前 `TaskExecutionLog` 的模块输出缓冲（复用
  runner 每模块的进度持久化机制），前端现有 `output` 轮询接口**零改动**
  即可看到 agent 侧实时日志；
- 模块输出中 agent 组段落加标头，如 `===== [via agent nat-gw-sh01] =====`。

### 4.5 ASGI 接入

- 引入 `channels` + `channels-redis`（Redis 已是 celery 依赖，无新增中间件）；
- 部署改为 ASGI 服务器（daphne 或 uvicorn）承载 `/ws/`，HTTP 部分不变
  （可 nginx 按路径分流：`/ws/` → ASGI，其余 → WSGI 现状）；
- `AgentConsumer` 职责：鉴权、register、心跳落库、dispatch 转发、
  log/result 落库与 ack、断线置 offline。

### 4.6 Jenkins 方法的通道解析（无目标主机）

Jenkins 构建不 SSH 任何主机，主机三态解析用不上，改由
`agent_mgr.channel.resolve_service_agent` 四级解析：

```
模块参数 params.agent  →  方法配置 config.agent  →  系统配置 jenkins.agent
  →  AgentRoute(任务/模块的项目组 + 环境)  →  直连
```

- 名字写了却查不到 agent → 抛 `ChannelError`，模块直接失败。**不静默回退
  直连**：公网 manager 直连内网 Jenkins 只会换来一次连接超时；
- 模块 `use_custom_scope=True` 时按模块维度解析，否则按任务全局维度；
- `params.agent` 与 `job_name`/`artifact_version` 同为平台保留键，不透传给
  Jenkins；
- 解析不出 agent 时走原直连路径（`method_mgr/executors/jenkins.py` 自己发
  REST，连接信息读系统配置 `jenkins.*`），存量部署行为不变。

### 4.7 管理与可视化

- 「Agent 管理」页：列表（名称/状态/版本/最近心跳/运行任务数）、创建
  （生成 token，一次性展示）、禁用/删除；
- 心跳离线告警：复用系统现有审计/通知机制，v1 至少落审计日志；
- 权限：仅管理员（与凭据管理同级）。

---

## 5. Agent 侧设计

### 5.1 技术选型

| 项 | 选择 | 理由 |
|------|------|------|
| 语言 | Python 3.10+ | 出口机必装 ansible（Python 生态），无额外运行时；与团队栈一致 |
| 连接 | `websockets`（asyncio） | 单连接场景最轻 |
| 执行 | `ansible-runner` | 官方库；产物目录自带结构化留存（stdout + 逐 event JSON + 每主机结果）；event 回调天然对接日志分片回传 |
| 打包 | venv + systemd | 单机部署，无需二进制分发 |

### 5.2 目录结构

```
/opt/ops-agent/
├── ops_agent/
│   ├── main.py          # 入口：加载配置、启动事件循环
│   ├── connection.py    # 连接管理：握手/心跳/重连/收发
│   ├── executor.py      # 任务执行：ansible-runner 封装、超时强杀、取消
│   ├── spool.py         # 可靠回传：日志分片落盘、ack 游标、重连补传
│   └── config.py        # 配置加载与校验
├── config.yaml
└── venv/

/var/lib/ops-agent/
├── spool/<job_id>/      # 未确认的日志分片与 result（ack 后删除）
└── artifacts/<job_id>/  # ansible-runner 产物归档（stdout / job_events / rc）

/var/log/ops-agent/
└── agent.log            # agent 自身日志（JSON lines，轮转）
```

### 5.3 配置（config.yaml）

```yaml
manager_url: wss://ops.example.com/ws/agent/
token: "<agent_token>"
agent_name: nat-gw-sh01

heartbeat_interval: 30
max_concurrent_jobs: 2        # 超出排队，队列上限 10，再满即 ack 拒收
default_forks: 10             # payload 未指定时的 ansible 并发上限
job_timeout_max: 3600         # payload timeout 的封顶值

ansible_project_dir: /opt/ansible-project
project_sync_cmd: "git pull --ff-only"

log_retention_days: 30        # artifacts 留存天数
disk_min_free_pct: 15         # 低于水位：拒收新任务 + 提前清理旧 artifacts
log_chunk_lines: 50           # 日志分片行数
log_flush_interval: 1.0       # 秒，行数不满也定时冲刷
```

### 5.4 进程模型与执行流程

- 主进程 asyncio 事件循环：连接管理 + 心跳 + 消息路由；
- 每个任务一个 `ansible-runner` 子进程（独立进程组），event 回调把日志行
  推入 asyncio 队列，由回传协程分片发送；
- 执行流程：

```
task.dispatch 收到
  → 校验（并发/队列/磁盘水位）→ task.ack(accepted 或拒收)
  → spool 落 payload（崩溃恢复依据）
  → project_sync（playbook 模块且开启时）
  → ansible-runner 启动子进程
      ├─ event → 本地 artifacts 落盘 + log.chunk 回传（spool 暂存至 ack）
      ├─ 超时 → kill 进程组，rc=124
      └─ task.cancel → kill 进程组，rc=143
  → task.result 发送，等 result.ack 后清理 spool
```

- 崩溃恢复：agent 重启时扫描 spool，`running` 状态的任务标记失败上报
  （子进程已随进程组消亡），未 ack 的日志/result 继续补传。

### 5.5 日志留存

| 日志 | 位置 | 格式 | 留存 |
|------|------|------|------|
| agent 运行日志 | `/var/log/ops-agent/agent.log` | JSON lines（时间/级别/事件/job_id） | RotatingFileHandler，50MB × 5 |
| ansible 执行日志 | `/var/lib/ops-agent/artifacts/<job_id>/` | ansible-runner 原生产物（stdout、job_events/*.json、rc、status） | 定时清理超 `log_retention_days`；磁盘低水位提前清 |
| 回传缓存 | `/var/lib/ops-agent/spool/<job_id>/` | 分片 JSON + ack 游标 | result.ack 后即删 |

### 5.6 资源控制

systemd 单元硬限制（不依赖代码自觉）：

```ini
[Unit]
Description=ops-agent
After=network-online.target

[Service]
User=ops-agent
ExecStart=/opt/ops-agent/venv/bin/python -m ops_agent.main
Restart=always
RestartSec=5
MemoryMax=512M
CPUQuota=100%
TasksMax=256
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

应用层限制：

- 并发任务数 `max_concurrent_jobs`（建议 1-2），队列满即 ack 拒收，
  manager 侧按失败处理；
- `forks` 封顶（payload 值与 `default_forks` 取小）；
- 单任务超时封顶 `job_timeout_max`，到时 kill 进程组；
- 日志回传缓冲行数上限（如 10 万行）——疯狂输出的任务截断并标注，防止
  内存/磁盘被单任务打爆；
- 磁盘水位低于 `disk_min_free_pct` 拒收新任务。

### 5.7 安全

- agent 零监听端口，只出不进；
- wss 强制 TLS，校验 manager 证书（配置可指定 CA）；
- token 泄露处置：manager 侧禁用/重置该 AgentNode 即断连；
- 只执行 `task.dispatch` 定义的单元类型（command / playbook / cert_deploy /
  jenkins），无任意代码加载路径；剧本路径限制在 `ansible_project_dir` 内
  （复用 `resolve_within` 同款校验），cert_deploy 的剧本由 agent 本地按固定
  模板生成（见 3.4.2）；
- SSH 私钥与 Jenkins 凭据仅驻留 agent 主机，通道与 manager 均不经手；
- 唯一经通道下发的秘密是证书内容（cert_deploy），落盘面按 3.4.2 逐个封住。

---

## 6. 任务状态机（agent 侧 job）

```
received ──ack拒收──▶ rejected
   │
   ▼
queued ──▶ syncing ──▶ running ──▶ uploading ──▶ done
                          │            ▲ result.ack 前断线：重连重发
                          ├─ timeout ─▶ failed(rc=124)
                          ├─ cancel ──▶ cancelled(rc=143)
                          └─ 非零 rc ─▶ failed
```

manager 侧不新增任务状态：agent 组的终态映射为该模块该组的 rc，汇入现有
steps_result / 失败处理（retry/skip/abort）流程。

---

## 7. 实施阶段

| 阶段 | 内容 | 交付 |
|------|------|------|
| v1 | Channels 接入 + AgentNode 模型/管理页 + Server.agent 通道字段 + runner 通道分组下发 + agent（心跳/command/playbook/日志回传/留存/资源限制） | NAT 主机可经 agent 执行任务流，日志前端实时可见 |
| v1.1 | task.cancel 取消、崩溃恢复补传、磁盘水位清理 | 可靠性补齐 |
| P1（2026-08-24） | 主机通道三态 + 项目组/环境路由规则 + 有效通道展示 | 新增主机自动继承通道，管理服可钉死直连 |
| P4（2026-08-28） | 证书分发经 agent：`step_type='cert_deploy'` 单元 + payload 敏感字段脱敏（manager `AgentJob.payload` 与 agent spool 都不落证书） | 内网主机的证书可自动续签自动分发，不必再改「强制直连」 |
| P2（2026-08-25） | Jenkins 代触发：`step_type='jenkins'` 单元 + agent 侧 REST 客户端 + register capabilities + 四级通道解析 + 系统配置 `jenkins.agent` | 公网 manager 可触发内网 Jenkins，构建日志实时回传 |
| v2 | 剧本 commit 校验、ansible_playbook method 经 agent、多 Jenkins 实例（payload 带实例名）、agent 重启后按 build_url 续跟构建、多 agent 冷备切换、心跳指标进 dashboard | 能力扩展 |

### 现有代码改造点清单

| 位置 | 改动 |
|------|------|
| `requirements.txt` | + channels、channels-redis、（agent 侧独立 requirements：websockets、ansible-runner） |
| `ops_manager/asgi.py` + 部署 | ASGI 路由 `/ws/agent/`，nginx 分流 |
| `apps/agent_mgr/`（新） | AgentNode 模型、Consumer、管理 API |
| `apps/asset_mgr/models.py` | Server + `agent` FK、`channel_mode` 三态（迁移），序列化器/表单 |
| `apps/agent_mgr/channel.py` | 通道解析唯一口径（runner / 证书分发 / 序列化共用） |
| `apps/task_exec/runner.py` | 模块主机按通道分组；agent 组走 dispatch 等待，输出拼接 |
| `apps/method_mgr/executors/jenkins.py` | 直连/经 agent 双路：解析出 agent 则打 `step_type='jenkins'` payload 下发（P2） |
| `apps/cloud_mgr/deploy.py` | 直连/经 agent 双路：解析出 agent 则打 `step_type='cert_deploy'` payload 下发（P4） |
| `apps/agent_mgr/dispatch.py` | `secret_fields` 参数：敏感 payload 字段只下发不落库（P4） |
| `ops-agent/ops_agent/executor.py` | cert_deploy 剧本本地生成 + 私有临时目录 + spool 脱敏（P4） |
| `ops-agent/ops_agent/jenkins.py` | agent 侧 Jenkins REST 客户端与执行句柄（P2） |
| `frontend` | Agent 管理页；服务器表单「执行通道」项 |
| `ops-agent/`（新仓库或本仓库子目录） | agent 全部代码 |

---

## 8. 开放问题（已确认，2026-07-27）

1. agent 代码：**独立仓库**（`~/workspace/ops-agent`）。
2. 出口主机到内网主机 **SSH 已免密**（密钥驻留 agent 主机，符合 3.4 设计）。
3. 生产 **nginx 前置**：`deploy/nginx.conf` 已加 `/ws/` → daphne(8001) 分流，
   daphne 托管配置 `deploy/ws-ops-manager.conf`（supervisor）。
