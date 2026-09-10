# 项目架构图

> 本文是**全局视角**的架构说明：进程怎么摆、请求怎么走、模块怎么依赖、外部系统怎么接。
> 单模块的内部设计见 [docs/backend/modules/](backend/modules/)，ops-agent 协议细节见
> [ops-agent-design.md](ops-agent-design.md)。

---

## 1. 系统全景

```mermaid
flowchart TB
    subgraph CLIENT["接入方"]
        BROWSER["运维/研发浏览器<br/>Vue 3 SPA"]
        CI["Jenkins CI 脚本<br/>X-API-KEY 上报制品"]
        MACHINE["机读客户端 / Webhook<br/>X-API-KEY + 签名"]
    end

    NGINX["nginx<br/>TLS 终止 · 静态资源 · 反代<br/>/api /admin → 8000 · /ws/ → 8001<br/>/_artifacts/ 内部重定向发包"]

    subgraph APP["ops-manager 应用层（同一份代码，四类进程）"]
        WSGI["gunicorn (WSGI) :8000<br/>gthread 4×8 preload<br/>全部 REST API"]
        WS["daphne (ASGI) :8001<br/>Channels：/ws/agent/ 接入<br/>/ws/task-logs/&lt;id&gt;/ 日志推送"]
        CEXEC["celery worker -Q task_exec<br/>concurrency=1<br/>任务流执行"]
        CDEF["celery worker (默认队列)<br/>concurrency=2<br/>证书签发/续签/分发"]
        BEAT["celery beat<br/>每 24h 扫临期证书续签"]
    end

    subgraph DATA["数据面"]
        DB[("MySQL（生产）<br/>SQLite（开发）")]
        REDIS[("Redis<br/>celery broker/result<br/>channel layer<br/>限速计数缓存")]
        FS[["文件<br/>ARTIFACT_ROOT 制品包<br/>logs/ 执行日志文件"]]
    end

    subgraph EXT["外部系统"]
        JENKINS["Jenkins<br/>触发 job + 拉控制台日志"]
        JMS["JumpServer<br/>资产注册"]
        DNS["阿里云 / 腾讯云<br/>DNS OpenAPI"]
        ACME["ACME (Let's Encrypt)<br/>DNS-01 签发"]
    end

    subgraph TARGET["被管资源"]
        DIRECT["公网可达主机<br/>ansible over SSH"]
        AGENT["ops-agent（NAT 出口机）<br/>wss 主动外连"]
        INNER["NAT 内网主机<br/>由 agent 侧 SSH 触达"]
    end

    BROWSER --> NGINX
    CI --> NGINX
    MACHINE --> NGINX
    NGINX --> WSGI
    NGINX --> WS

    WSGI --> DB
    WSGI --> REDIS
    WSGI --> FS
    CEXEC --> DB
    CEXEC --> REDIS
    CEXEC --> FS
    CDEF --> DB
    CDEF --> REDIS
    WS --> DB
    WS --> REDIS
    BEAT --> REDIS

    WSGI -. "投递任务" .-> REDIS
    REDIS -. "消费" .-> CEXEC
    REDIS -. "消费" .-> CDEF

    CEXEC --> DIRECT
    CEXEC --> JENKINS
    CEXEC --> JMS
    CDEF --> DNS
    CDEF --> ACME
    WSGI --> DNS

    AGENT -- "wss（方向恒为 agent → manager）" --> NGINX
    WS <-. "指令下发 / 日志回传" .-> AGENT
    AGENT --> INNER
```

要点：

- **入口只有 nginx 一个**：HTTP 走 gunicorn，`/ws/` 单独分流到 daphne；两者是不同进程、不同端口，缺一个就少一半能力（缺 daphne 时前端退回轮询日志）。
- **写请求不在 web 进程里干活**：web 只落库 + 置状态 + 投 Redis，真正的 ansible/acme 在 celery worker 里跑。worker 没起 = 任务永远排队且页面无报错。
- **两个 celery worker 不共队列**：`task_exec` 队列跑任务执行，默认队列跑证书，互不抢占；同机运行必须给不同 `-n` node 名。
- **连接方向恒为 agent → manager**，manager 从不主动连 agent，NAT 内网主机零改造。

---

## 2. 进程与端口

| 进程 | 启动方式 | 端口 | 职责 | 不部署的后果 |
|------|----------|------|------|--------------|
| nginx | 系统服务 / docker | 80, 443 | TLS、静态资源、反代、`X-Accel-Redirect` 发制品 | 无入口 |
| gunicorn | `deploy/ops-manager.conf` | 127.0.0.1:8000 | 全部 REST API、Django Admin | API 全挂 |
| daphne | `deploy/ops-manager-ws.conf` | 127.0.0.1:8001 | `/ws/agent/`、`/ws/task-logs/<id>/` | agent 掉线；日志退回轮询 |
| celery worker (exec) | `deploy/ops-manager-celery-exec.conf` | — | `-Q task_exec`，跑任务流 | 任务卡在 pending |
| celery worker (默认) | `deploy/ops-manager-celery.conf` | — | 证书签发/续签/分发 | 证书永远「申请中」 |
| celery beat | `deploy/ops-manager-celery-beat.conf` | — | 24h 扫临期证书 | 不自动续签（可手动） |
| Redis | 系统服务 | 6379 | broker / result / channel layer / 缓存 | celery 与 WebSocket 均不可用 |
| MySQL | 系统服务 | 3306 | 主数据 | 全挂 |

> 生产用 **supervisor** 托管（非 systemd），配置见 `deploy/*.conf`。

---

## 3. 请求生命周期

```mermaid
flowchart LR
    REQ["请求"] --> MW
    subgraph MW["中间件链（顺序即执行顺序）"]
        direction TB
        M1["SecurityMiddleware"] --> M2["CorsMiddleware"] --> M3["SessionMiddleware"]
        M3 --> M4["CommonMiddleware"] --> M5["CsrfViewMiddleware"] --> M6["AuthenticationMiddleware"]
        M6 --> M7["MessageMiddleware"] --> M8["XFrameOptionsMiddleware"] --> M9["AuditLogMiddleware<br/>写操作自动留痕"]
    end
    MW --> AUTH
    subgraph AUTH["认证层（DRF）"]
        direction TB
        A1["MfaJWTAuthentication<br/>JWT + MFA 强制（读 token 的 amr，不查库）"]
        A2["ApiKeyAuthentication<br/>X-API-KEY → 密钥比对 → 过期 → IP 白名单 → 验签 → 作用域"]
    end
    AUTH --> PERM["权限层<br/>IsAuthenticated / IsAdmin / IsAdminOrReadOnly<br/>IsAdminOrReadCreate + 角色矩阵 + 数据范围"]
    PERM --> VIEW["Router → ViewSet → Serializer → ORM"]
    VIEW --> DB[("DB")]
    VIEW --> RESP["统一分页 / 统一异常响应<br/>common.pagination · common.response"]
```

两条关键口径：

- **MFA 强制放在认证类**而不是权限类：大量 ViewSet 自带 `permission_classes`，全局默认权限对它们不生效，放权限层会漏掉一半接口。
- **机读接口自带 `ApiKeyAuthentication`**，因而天然绕过 MFA —— 这正是期望行为（脚本无法过第二因子）。

---

## 4. 后端模块依赖

```mermaid
flowchart TB
    subgraph L1["接入与安全"]
        AUTHM["auth_mgr<br/>用户 · 角色 · JWT · MFA · API 凭证"]
        AUDIT["audit<br/>写操作审计（中间件）"]
        SYS["system_cfg<br/>全局键值配置"]
    end

    subgraph L2["CMDB / 台账"]
        ASSET["asset_mgr<br/>Server · ServerService<br/>五字典：项目组/渠道/环境/云厂商/服务类型"]
        CONF["config_mgr<br/>归属批量分配（无模型）"]
    end

    subgraph L3["执行面"]
        METHOD["method_mgr<br/>Method + provider 执行器<br/>ansible_playbook / jenkins / jumpserver / cloud_host"]
        EXEC["task_exec<br/>ExecutionTask · TaskStep · 变量 · 凭据<br/>runner / trigger / webhook / 日志推送"]
        AGENTM["agent_mgr<br/>AgentNode · AgentRoute · AgentJob<br/>通道解析 + WS 下发"]
        ART["artifact_mgr<br/>Artifact 上报 / 签名下载"]
    end

    subgraph L4["云控与视图"]
        CLOUD["cloud_mgr<br/>域名 · 解析（审批/风险）· 证书 · 分发"]
        TOPO["topo_mgr<br/>画布 · 节点 · 边"]
        DASH["dashboard<br/>值班视角聚合（无模型）"]
        PORTAL["portal_mgr<br/>内部导航页配置"]
    end

    COMMON["common<br/>pagination · response · permissions · dimscope<br/>ansible · jumpserver · crypto/secrets · totp · ratelimit"]

    CONF --> ASSET
    EXEC --> ASSET
    EXEC --> METHOD
    EXEC --> AGENTM
    EXEC --> ART
    METHOD --> ASSET
    AGENTM --> ASSET
    ART --> ASSET
    CLOUD --> ASSET
    CLOUD --> AGENTM
    TOPO --> ASSET
    TOPO --> CLOUD
    DASH --> ASSET
    DASH --> EXEC
    DASH --> CLOUD
    DASH --> TOPO
    DASH --> AUDIT

    L1 -.-> COMMON
    L2 -.-> COMMON
    L3 -.-> COMMON
    L4 -.-> COMMON
```

依赖只朝一个方向流：**台账 ← 执行面 ← 聚合视图**。`dashboard` / `topo_mgr` 只读别人的表，不建自己的台账；`config_mgr` / `dashboard` 无模型。

---

## 5. 任务执行链路

一个「任务」= 一条**任务流**：多个 TaskStep 模块顺序执行，任一模块失败即中止。

```mermaid
sequenceDiagram
    autonumber
    participant U as 前端
    participant W as gunicorn (API)
    participant R as Redis
    participant C as celery -Q task_exec
    participant A as ops-agent
    participant H as 目标主机
    participant D as daphne (/ws/task-logs/)

    U->>W: POST /api/exec/tasks/ 或 {id}/run
    W->>W: 落库 + 公共前置校验与快照（trigger.py）
    W->>R: apply_async(queue=task_exec)
    W-->>U: 202 + task_id（保存与执行分离）
    U->>D: 订阅 /ws/task-logs/<task_id>/

    loop 逐个 TaskStep
        C->>C: 按维度实时匹配主机（全局 + 模块自定义）
        C->>C: 校验主机非空 + 方法服务规划（逐模块）
        C->>C: 解析执行通道（主机三态 + 项目组/环境路由规则）
        alt 直连
            C->>H: ansible / ansible-playbook over SSH
            H-->>C: 输出
        else 经 agent
            C->>R: dispatch → channel layer
            R->>A: WS 指令下发
            A->>H: ansible over SSH（内网）
            A-->>R: 日志分片 + ack 回传
            R-->>C: 结果
        end
        C->>R: 日志分片推送
        R->>D: channel layer
        D-->>U: WebSocket 实时输出
        C->>C: 落主机快照 + 逐模块结果
        break 模块失败
            C->>C: 中止后续模块（可暂停等人工处理）
        end
    end
```

- **保存与执行分离**：编排时不锁主机，执行时才按维度实时匹配。
- **通道解析统一口径**（`apps/agent_mgr/channel.py`）：runner 分组、方法模块拦截、证书分发、服务器列表展示共用同一函数。
- **Jenkins 类模块**无目标主机，走 `resolve_service_agent` 四级解析（模块参数 → 方法配置 → `jenkins.agent` 系统配置 → 项目组/环境规则）。
- 日志双写：WebSocket 实时推 + 落 `logs/` 文件，断连后可回看。

---

## 6. CI/CD 与制品链路

```mermaid
flowchart LR
    JOB["Jenkins job"] -- "构建产出压缩包" --> UP
    UP["POST /api/artifacts/report/<br/>X-API-KEY + 版本/md5<br/>项目组·渠道·环境·服务类型"] --> STORE[("ARTIFACT_ROOT")]
    TRIG["任务流 jenkins 模块<br/>（经 agent 代触发，凭据留 agent 本机）"] --> JOB
    JOB -- "控制台日志增量同步" --> LOGV["平台执行日志"]
    STORE --> CD["CD 任务执行时<br/>自动注入 package_* 变量"]
    CD --> DL["签名下载 URL<br/>nginx X-Accel-Redirect 发文件"]
    DL --> HOST["目标主机取包部署"]
```

取包按**服务类型严格匹配、不回退**：Jenkins 上报缺 `service_type` 参数会导致 CD 取不到包，而不是错发上一个类型的包。

---

## 7. 云控链路（域名 / 证书）

```mermaid
flowchart TB
    DOM["Domain 主域名"] --> REC["DnsRecord 解析记录"]
    REC --> RISK{"高危判定<br/>dns_risk"}
    RISK -- "命中" --> APPROVE["DnsChangeRequest 待审批<br/>必须填变更原因"]
    RISK -- "未命中" --> APPLY
    APPROVE -- "通过" --> APPLY["调用云厂商 DNS OpenAPI<br/>providers/aliyun · tencent（零 SDK）"]
    APPLY --> HIST["DnsChangeLog 变更历史"]

    CERT["Certificate"] -- "celery 默认队列" --> ACME["acme.sh DNS-01<br/>ACME_TIMEOUT=900s"]
    ACME --> ISSUED["已签发"]
    BEAT["celery beat 24h"] -- "距过期不足 CERT_RENEW_BEFORE_DAYS 天" --> ACME
    ISSUED --> DEPLOY["CertDeployment 分发<br/>直连或经 agent 内联下发（三处脱敏）"]
    DEPLOY --> NODE["目标主机 / 负载均衡"]
```

`issuing` 状态卡死靠**读时超时判定**自愈（worker 被砍时状态不会永远停在申请中）。

---

## 8. 数据域概览

| 域 | 主要模型 | 说明 |
|----|----------|------|
| 身份 | `Role` `User` `UserMFA` `MFARecoveryCode` `ApiCredential` | 角色矩阵 + 生产执行能力位 + 数据范围；MFA 密钥 Fernet 加密；API 凭证带 per-key IP 白名单/作用域/签名 |
| CMDB | `ProjectGroup` `Channel` `Environment` `CloudProvider` `ServiceType` `Server` `ServerService` | 五字典 + 服务器 + 进程规划（服务类型三态） |
| 执行 | `Method` `ExecutionTask` `TaskStep` `TaskExecutionLog` `Credential` `ExecVariable` `ExecVariableVersion` | 任务流 + 变量版本 + 凭据（密钥不回显） |
| 通道 | `AgentNode` `AgentRoute` `AgentJob` | 主机三态 + 路由规则 + 下发作业 |
| 制品 | `Artifact` | 版本/md5/维度四元组 + 服务类型 |
| 云控 | `CloudAccount` `Domain` `DnsRecord` `DnsChangeLog` `DnsChangeRequest` `Certificate` `CertDeployment` | 域名 / 解析 / 审批 / 证书 / 分发 |
| 视图 | `TopoCanvas` `TopoNode` `TopoEdge` | 引用型节点实时读原表，外部型手录 |
| 其它 | `SystemConfig` `PortalSetting` 等 | 全局配置、内部导航页 |

字段级说明见 [backend/models.md](backend/models.md)。

---

## 9. 前端结构

```mermaid
flowchart LR
    MAIN["main.js"] --> ROUTER["router/<br/>路由 + 菜单（按角色矩阵裁剪）"]
    MAIN --> PINIA["stores/<br/>用户 · token · 偏好"]
    ROUTER --> LAYOUT["views/Layout.vue"]
    LAYOUT --> V1["views/assets · config"]
    LAYOUT --> V2["views/task"]
    LAYOUT --> V3["views/cloud"]
    LAYOUT --> V4["views/topo<br/>Vue Flow + dagre"]
    LAYOUT --> V5["views/audit · system"]
    LAYOUT --> V6["views/Dashboard.vue"]
    V1 & V2 & V3 & V4 & V5 & V6 --> API["api/<br/>接口调用层"]
    API --> AXIOS["utils/request<br/>Axios 封装：token 注入 · 401 刷新 · 统一报错"]
    AXIOS --> BE["/api/*"]
    V2 -. "WebSocket" .-> WS["/ws/task-logs/&lt;id&gt;/"]
```

详见 [frontend/overview.md](frontend/overview.md)。

---

## 相关文档

- [后端架构总览](backend/overview.md) · [前端架构总览](frontend/overview.md)
- [API 接口文档](backend/api-reference.md) · [数据模型](backend/models.md) · [配置说明](backend/configuration.md)
- [ops-agent 设计](ops-agent-design.md) · [部署说明](../deploy/README.md)
