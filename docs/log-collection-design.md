# 日志采集设计（Vector → VictoriaLogs）

> 目标：把散在各机器、各进程的日志统一成结构化格式，由 Vector 采集、推送到
> VictoriaLogs，做到「一个 request_id 查出全链路」「按 level/status/task_id 直接过滤」。
>
> 状态：**P0 已落地**（统一格式），P1-P3 待做。改动清单见文末「分期」。

## 1. 现状盘点

改造前的日志源与格式：

| 源 | 路径 | 原格式 | 问题 |
|---|---|---|---|
| Django 应用 | `logs/ops_manager.log`、`logs/error.log` | `{levelname} {asctime} {name} {module}:{lineno} {message}` | 无时区；traceback 多行；无请求关联；**同机多进程写同一文件** |
| gunicorn access | `~/logs/gunicorn-access.log` | `%(t)s %(m)s %(U)s?%(q)s %(s)s %(M)sms pid=` | 空格分隔，URI 里带空格即错位 |
| gunicorn stdout/stderr | `~/logs/opsmgr.log`、`-error.log` | 同 Django（console handler） | **与 file handler 内容重复，采集后存两份** |
| celery ×3 | `~/logs/opsmgr-celery*.log` | celery 前缀 + Django 格式混排 | 同一文件两种格式 |
| daphne / feishu-ws | `~/logs/opsmgr-ws.log`、`logs/feishu-ws.log` | 同上 | 同上 |
| nginx access | `/var/log/nginx/opsmgr*.access.log` | 自定义 `ops_timing` | 引号嵌套，UA 里带引号即解析失败 |
| nginx error | `/var/log/nginx/opsmgr.error.log` | nginx 内置 | 多行，无法 JSON 化 |
| 任务执行 | `logs/tasks/<task_id>/<exec_id>.log` | ansible 原始输出 | 含 ANSI 颜色码、多行、**可能含下发的凭据** |
| 审计 | DB 表 `audit_auditlog` | 结构化但在库里 | 不在文件流上 |
| ops-agent | 独立仓库，NAT 出口机 | 另行确认 | 跨网络边界 |

四套时间格式并存：`2026-09-17 14:10:05`（无时区）、`04/Sep/2026:15:49:35 +0800`、
celery 默认格式、ansible 无时间戳。

## 2. 统一格式：JSON Lines

一条日志 = 一行 JSON。VictoriaLogs 的数据模型只有三个保留字段
（`_time`、`_msg`、`_stream`），其余字段自动索引，所以字段名怎么起都行，
关键是**哪些字段进 `_stream`**。

示例：

```json
{"ts":"2026-09-17T06:10:05.123Z","level":"ERROR","service":"celery-exec","logger":"apps.task_exec.runner","module":"runner","line":214,"msg":"任务执行失败","request_id":"9f2c...","task_id":12,"exec_id":43,"exc":"Traceback (most recent call last):\n..."}
```

### 字段分两类，分错会炸性能

**stream 字段（低基数，进 `_stream`）**：`host`、`app`、`service`、`env`。
VictoriaLogs 按 stream 组织存储，组合数应控制在几百以内。

**普通字段（高基数，不进 stream）**：`request_id`、`user`、`remote_addr`、
`task_id`、`exec_id`、`status`、`rt`。

> ⚠ `request_id` / `task_id` / IP **绝不能**做 stream 字段。每出现一个新值就建一条
> 新 stream，几天就是百万级 stream，写入和查询一起垮。这是接 VictoriaLogs
> 最容易踩且最难回滚的一个坑（已写入的 stream 结构改不了，只能等过保留期）。

### 谁出哪些字段

- **进程出**：`ts`、`level`、`service`、`logger`、`module`、`line`、`msg`、
  `request_id`，以及 `extra=` 传入的业务字段。
- **Vector 出**：`host`、`app`、`env`。进程不该知道自己属于哪个环境——读环境变量
  等于把部署信息散进代码；Vector 配置本来就按机器分发，放那边只有一处。

时间统一 RFC3339 + 毫秒 + `Z`（UTC）。查询侧再转本地时区。

## 3. 各源改造

### 3.1 Django / celery / daphne（P0 已做）

`common/logging.py` 新增 `JSONFormatter`，零新依赖。要点：

- **traceback 进 `exc` 字段**，不是拼在消息后换行。一条异常永远是一行，
  采集端不需要靠「下一行是否以时间戳开头」猜多行边界——猜错就把半截栈丢给别的日志。
- **`extra=` 的键原样进 JSON**。`logger.info('执行失败', extra={'task_id': 12})`
  在服务端就是可查询字段，不用再正则抠消息文本。用排除法（剔掉 LogRecord 内置属性）
  而不是要求调用方写 `extra={'extra_fields': {...}}`，这样 celery/Django 三方库
  自己塞的字段也能一并收下。
- **`default=str`**：extra 里塞 datetime/Model/Exception 是常态，一个不可序列化的值
  不该让整条日志消失。
- **`ensure_ascii=False` + 紧凑分隔符**：中文占位从 3 倍降回 1 倍，服务端直接可读。

开关：`LOG_FORMAT=json|verbose`。prod 默认 `json`，dev 默认 `verbose`（本地 tail
不用过 jq）。控制台 handler 永远是 verbose，不受开关影响。

**日志文件按进程拆分**：`logs/ops_manager-<service>.log`、`logs/error-<service>.log`，
`service` 由 `LOG_SERVICE` 给（各 supervisor 配置注入：`django` / `ws` /
`celery` / `celery-beat` / `celery-exec` / `feishu-ws`）。

> 这不是为了好看。homelab 那台机上 gunicorn、daphne、celery-exec 三个进程各自持有
> 一个 `RotatingFileHandler` 指向同一个 `logs/ops_manager.log`，轮转时各做各的
> rename + 重开，**互相覆盖，轮转瞬间的日志直接消失**。单进程时看不出来，多进程下
> 是必然丢数据。这是采集之前必须先解决的，否则采到的本身就是残缺的。

**去掉 prod 的 console handler**：supervisor 已把进程 stdout 落到
`~/logs/opsmgr.log`，file handler 又把同样内容写进 `logs/ops_manager-django.log`，
两个文件都采 = 每条日志在 VictoriaLogs 里存两份。保留 file handler（JSON、自带轮转），
去掉 console 后 supervisor 那份只剩 gunicorn/celery 自身的启动与崩溃输出——低频，
且正是 file handler 覆盖不到的部分，仍然要采。

> 副作用：prod 上跑 `manage.py xxx` 不再有终端输出，结果去 `logs/` 看。
> 临时要看设 `LOG_FORMAT=verbose` 不解决这个（那只改格式），需要时单独加回 console。

### 3.2 请求 ID 串联（P0 已做）

```
nginx $request_id ──┬─→ access log 的 rid 字段
                    └─→ X-Request-ID 头 ─→ Django RequestIDMiddleware
                                             ├─→ 每条应用日志的 request_id 字段
                                             ├─→ gunicorn access log 的 rid 字段
                                             └─→ 响应头 X-Request-ID（前端报错可截图带走）
```

一次请求在 nginx / gunicorn / Django 三处的记录用同一个值串起来，
`request_id:xxx` 一把捞出全链路，不再靠时间戳附近瞎找。

实现要点：

- `contextvars` 而非 `threading.local`。gunicorn 跑 gthread（线程）、daphne 跑
  asyncio（协程），`threading.local` 在协程下会串味——同线程多个协程共用一份。
- **`finally` 里必须 reset**。gthread 线程是复用的，不还原会让下一个请求继承上一个的
  ID。请求间串号比没有 ID 更糟：排障会追到完全无关的链路上。
- 中间件放 `MIDDLEWARE` 第一位，其后的认证、审计中间件产生的日志才带得上 ID。

**安全**：`X-Request-ID` 在 nginx 侧是 `proxy_set_header` 覆盖语义，客户端自带的同名头
到不了后端。但 Django 也可能被直连（本地调试、内网旁路），那时该头是客户端可控输入，
因此中间件对它做形状校验（`^[A-Za-z0-9_-]{1,64}$`），不合法直接丢弃换新生成的 ID。
不校验就等于让外部往日志字段里写任意内容：超长串撑存储、控制字符污染下游解析。

### 3.3 nginx（P0 已做）

`log_format ops_json escape=json`，字段与原 `ops_timing` 一一对应，加 `rid`。

> `escape=json` 不能省。UA 和 referer 是客户端可控字符串，里面的引号/反斜杠不转义会
> 直接破坏 JSON 结构，采集端整行解析失败——而且这是攻击者可主动触发的。

耗时类字段（`rt`/`uct`/`uht`/`urt`）一律加引号按字符串出：nginx 对未命中的变量输出空串，
静态文件 location 下 `$upstream_*` 就是空，写成裸数字会产出 `"urt":,` 这种坏 JSON。
代价是查询时要用 `math` 转换才能做数值比较，远小于整行解析失败。

nginx error log 无法 JSON 化，P1 在 Vector 侧用正则 + multiline 合并处理。

### 3.4 gunicorn access（P0 已做）

改为单行 JSON，保留原有的 pid 列（判断「是否只有某个 worker 慢」的决定性一列），
新增 `rid`。

`ts_clf` 是 gunicorn 唯一的时间原子（`%(t)s`），Apache 格式带方括号，gunicorn 不提供
ISO 输出，因此原样出、由 Vector 解析成 `_time`：

```coffee
.ts = parse_timestamp!(strip_whitespace(replace(string!(.ts_clf), r'[\[\]]', "")), "%d/%b/%Y:%H:%M:%S %z")
```

所有字段加引号：status 在早期失败时会是 `-`，裸着出即坏 JSON。

> **已知缺口**：gunicorn 不做值转义（没有 nginx 的 `escape=json`）。URI 或 query
> 里真出现裸引号时这一行会是坏 JSON。前面有 nginx 把关（畸形请求行被 400 挡掉、
> 特殊字符通常已 percent 编码），概率低；真发生时会落进 Vector 的 `parse_error`
> 分支，原文仍然入库、可被查到，不会静默丢失。若实测有量，直接去掉 `args` 字段
> ——nginx access log 已经记了它，且是转义过的。

> 是否保留 gunicorn access log 可再议——nginx access log 已覆盖同样的请求，
> 唯一不可替代的是 pid。若日志量成为问题，这是第一个可以关掉的源。

### 3.5 任务执行日志（P2）

`logs/tasks/<task_id>/<exec_id>.log` 是 ansible 原始输出，接入前必须解决三件事：

1. **脱敏**。ansible 输出里会带下发的凭据、证书内容。落盘前过
   `apps/task_exec/live.py` 的 `mask_secrets`，**不能只在 Vector 侧做**——
   Vector 不知道哪些值是密钥，而落盘文件本身也是泄露面。
2. **ANSI 颜色码**。`TASK_LOG_COLOR=True` 时文件里带 `\x1b[1;35m`，Vector 侧剥掉
   （前端着色依赖它，所以是采集时剥，不是落盘时剥）。
3. **多行**。ansible 的 PLAY/TASK 块天然多行，按行采即可（每行带 `task_id`/`exec_id`
   字段），不做块合并——合并后单条过大，且对查询无益。

### 3.6 审计（P3）

`audit_auditlog` 已是结构化数据，且有独立的查询页面，不急。要接就在
`AuditLogMiddleware._record` 里同时往 `/insert/jsonline` 推一份，
注意复用已有的 `redact_body`——审计表脱敏过的内容，进日志库同样要脱敏。

### 3.7 ops-agent（P2）

在 NAT 出口机上，独立仓库。两种接法待定：

- **A：该机装 Vector 直推**。前提是它能出网到 VictoriaLogs（或经跳板）。简单，但多一条
  出网链路要开通与审计。
- **B：日志经既有 WebSocket 通道回传**，由本平台侧统一推。复用已有的加密通道与鉴权，
  不新开口子，但要在 agent 与 agent_mgr 两侧写传输逻辑，且长期占用通道带宽。

倾向 A（采集链路和业务链路不该耦合，业务通道拥塞时最需要日志）。落地前确认出网策略。

## 4. Vector 侧（P1）

**拓扑**：每台机一个 Vector agent 直推 VictoriaLogs。机器数是个位数，不架 aggregator——
aggregator 的价值在于收敛连接数和集中做转换，现在两者都不构成问题，多一层就多一个
故障点。

配置文件在 [`deploy/log-server/vector.yaml`](../deploy/log-server/vector.yaml)
（各机差异靠 systemd 的 `EnvironmentFile` 注入，不为每台机维护一份配置）。
安装步骤与权限要求见 [`deploy/log-server/README.md`](../deploy/log-server/README.md)。

要点：

- **磁盘 buffer 必须配**，且 `when_full = "block"`。默认是内存 buffer，Vector 一重启
  缓冲里的日志就没了——而「服务端不可用」和「需要重启 Vector」这两件事经常同时发生。
  block 而非 drop_newest：宁可让 Vector 卡住（表现为日志延迟，看得见），
  也不要静默丢日志（表现为什么都没有，看不见）。
- **解析失败不丢**：所有 source 的 remap 都有 `parse_error: true` 兜底分支，原文照样入库。
  `parse_error` 常年不为 0 = 还有日志源没纳入统一格式，这是验收项不是告警噪声。
- **只采 `ops_manager-*.log`，不采 `error-*.log`**：后者是前者中 ERROR 级的副本。
- `timezone = "Asia/Shanghai"` 是全局项：nginx error log 的时间戳不带时区，
  不设的话按 UTC 解析，直接差 8 小时。

用 Elasticsearch bulk 端点，是 VictoriaLogs 文档给出的 Vector 接法：批量、重试、
压缩都是 Vector 的 `elasticsearch` sink 现成的。`/insert/jsonline` 配 `http` sink 也能
写进去，但重试语义弱得多。若所用 Vector 版本已带原生 VictoriaLogs sink，可直接换，
字段映射逻辑一致。

## 5. VictoriaLogs 侧（P1）

systemd unit 在 [`deploy/log-server/victoria-logs.service`](../deploy/log-server/victoria-logs.service)，采集端配置见 [`deploy/log-server/README.md`](../deploy/log-server/README.md)。

- 单机 `victoria-logs` 二进制即可，关键启动参数：
  - `-retentionPeriod=30d`——先定 30 天，看实际磁盘增长再调；
  - `-retention.maxDiskSpaceUsagePercent=80`——保留期之外的硬兜底，磁盘写满
    比日志少存几天严重得多；
  - `-storageDataPath=/home/homelab/data/victoria-logs`——这台机的数据盘挂在 `/home` 下，不用 `/var/lib`；
    systemd 的 `ProtectHome=false` 与 `ReadWritePaths` 必须跟着一致。
- 查询：内置 vmui 在 `http://<内网IP>:9428/select/vmui/`；Grafana 装 VictoriaLogs datasource 插件。

> **认证：内网环境，已决定不做**（无鉴权层、无 basic auth、采集链路走 HTTP）。
>
> VictoriaLogs 自身没有任何认证机制，所以这个决定意味着访问控制完全落在网络
> 边界上：能连到 9428 的人即可读全部日志，也可往 `/insert/*` 写任意内容
> （伪造日志淹没真实记录——需要查的时候恰好查不到）。
>
> 因此剩下的唯一控制点是监听地址：`-httpListenAddr` 写具体内网 IP，不要写
> `:9428`（等价于 `0.0.0.0`）。日志服务器若有公网网卡，这一条就是公网与否的
> 分界线。部署后核 `ss -lntp | grep 9428`。
>
> 若日后要开到内网之外，把 nginx basic auth / vmauth 补回来再说。

## 6. 查询示例（LogsQL）

```logsql
# 某次请求的全链路（nginx + gunicorn + Django 三个 service 一起出）
request_id:"9f2c8a1e..." | sort by (_time)

# 最近 1 小时的应用报错，按 logger 聚合看热点
_time:1h _stream:{app="ops-manager"} level:ERROR | stats count() by (logger)

# 5xx 请求
_time:1h _stream:{service="nginx"} status:>=500

# 某次任务执行的全部日志
task_id:12 exec_id:43 | sort by (_time)

# 解析失败的行有多少（Vector 侧 parse_error 标记，用来验收格式统一是否彻底）
_time:24h parse_error:true | stats count() by (host, service)
```

## 7. 分期

| 期 | 内容 | 状态 |
|---|---|---|
| P0 | Django JSONFormatter、request_id 串联、日志文件按进程拆分、prod 去重、nginx/gunicorn JSON 格式 | **已完成** |
| P1 | 部署 VictoriaLogs（内网不做认证）、各机装 Vector、采应用 + nginx + gunicorn、nginx error log 解析 | 配置文件已备（`deploy/log-server/`），**未实机部署验证** |
| P2 | 任务执行日志（脱敏 + 剥 ANSI）、ops-agent 主机接入 | 待做 |
| P3 | 审计双写、Grafana 面板、vmalert 告警（ERROR 突增、5xx 比率） | 待做 |

### P0 改动清单

| 文件 | 改动 |
|---|---|
| `common/logging.py` | 新增：`JSONFormatter`、`RequestIDMiddleware`、request_id contextvar |
| `ops_manager/settings/base.py` | `json` formatter、`LOG_FORMAT`/`LOG_SERVICE`、日志文件按 service 拆分、中间件入栈 |
| `ops_manager/settings/prod.py` | 默认 json 格式、剥掉 console handler |
| `deploy/nginx/conf/templates/ops_manager.conf.template` | `ops_timing` → `ops_json`（`escape=json` + `rid`） |
| `deploy/nginx/conf/snippets/proxy_backend.conf` | 下发 `X-Request-ID: $request_id` |
| `deploy/opsmgr.conf` | gunicorn access log 改 JSON + `rid`；`LOG_SERVICE=django` |
| `deploy/ops-manager-{ws,celery,celery-beat,celery-exec,feishu-ws}.conf` | 各自的 `LOG_SERVICE` |
| `deploy/env.example` | `LOG_FORMAT` / `LOG_SERVICE` 说明 |
| `common/tests.py` | `JSONFormatterTests`、`RequestIdTests` |

### P0 上线步骤

1. 部署代码，`supervisorctl reread && supervisorctl update`（改了 environment 与
   command，必须 update，`restart` 不重读配置）。
2. nginx：**先 `nginx -t`**（本地无 nginx 环境，配置语法未经实机验证），
   通过后再 `nginx -s reload`。
3. 验收：
   - `tail -1 logs/ops_manager-django.log | jq .` 能解析，有 `service`、`request_id`；
   - `logs/` 下每个进程一个文件，无同名互相覆盖；
   - 请求任意接口，响应头有 `X-Request-ID`，其值同时出现在 nginx access log 的 `rid`、
     gunicorn access log 的 `rid`、应用日志的 `request_id`；
   - `~/logs/opsmgr.log` 不再有应用日志的重复行。

**回滚**：设 `LOG_FORMAT=verbose` 重启即可退回文本格式（文件名拆分与 request_id 不受
影响，两者都是纯增强）。nginx 侧回滚需要改回模板并 reload。

## 8. 未决事项

- 日志量级未实测，直接影响 VictoriaLogs 的磁盘规划与保留期。P1 第一步应该是
  「先采一天，量一量」，而不是先定保留期。
- ops-agent 的接法（3.7 的 A/B）需要先确认出网策略。
- gunicorn access log 是否保留（与 nginx access 高度重复，见 3.4）。
