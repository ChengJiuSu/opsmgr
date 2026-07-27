# ops-agent 设计文档

> 状态：v1 已实现（2026-07-27）。manager 侧模块文档：`docs/backend/modules/agent.md`；
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
| `register` | agent → mgr | `agent_name, version, hostname, capabilities` | 连接后首条消息，manager 校验后回 `register.ack` |
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

- `inventory` 直接下发文本，agent 落临时文件后传给 ansible，**复用 manager
  现有动态 inventory 口径**（主机名别名、`service_types` 变量、服务分组）；
- NAT 内主机 `conn_ip` 回退私网 IP 的现有逻辑正好适配：agent 在内网，
  私网 IP 可达；
- **SSH 凭据不下发**：私钥驻留 agent 主机（`~/.ssh` 或配置指定路径），
  manager 只下发 `ansible_user/ansible_port`（已在 inventory 内）。降低
  密钥在通道与 manager 库中暴露的面。

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

### 4.2 Server 增加执行通道

`asset_mgr.Server` 新增可空外键 `agent = FK(AgentNode, null=True)`：

- `agent is None` → 直连通道（现状，manager 本机跑 ansible）；
- `agent` 有值 → 该主机的 ansible 执行下发给对应 agent。

服务器表单增加「执行通道」选择（默认直连）。批量设置入口挂在服务器列表
批量操作。

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

### 4.6 管理与可视化

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
- 只执行 `task.dispatch` 定义的两类单元（command / playbook），无任意
  代码加载路径；剧本路径限制在 `ansible_project_dir` 内（复用
  `resolve_within` 同款校验）；
- SSH 私钥仅驻留 agent 主机，通道与 manager 均不经手。

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
| v2 | 剧本 commit 校验、method 模块经 agent（provider 透传）、多 agent 冷备切换、心跳携带指标进 dashboard | 能力扩展 |

### 现有代码改造点清单

| 位置 | 改动 |
|------|------|
| `requirements.txt` | + channels、channels-redis、（agent 侧独立 requirements：websockets、ansible-runner） |
| `ops_manager/asgi.py` + 部署 | ASGI 路由 `/ws/agent/`，nginx 分流 |
| `apps/agent_mgr/`（新） | AgentNode 模型、Consumer、管理 API |
| `apps/asset_mgr/models.py` | Server + `agent` FK（迁移），序列化器/表单 |
| `apps/task_exec/runner.py` | 模块主机按通道分组；agent 组走 dispatch 等待，输出拼接 |
| `frontend` | Agent 管理页；服务器表单「执行通道」项 |
| `ops-agent/`（新仓库或本仓库子目录） | agent 全部代码 |

---

## 8. 开放问题（已确认，2026-07-27）

1. agent 代码：**独立仓库**（`~/workspace/ops-agent`）。
2. 出口主机到内网主机 **SSH 已免密**（密钥驻留 agent 主机，符合 3.4 设计）。
3. 生产 **nginx 前置**：`deploy/nginx.conf` 已加 `/ws/` → daphne(8001) 分流，
   daphne 单元 `deploy/ops-manager-ws.service`。
