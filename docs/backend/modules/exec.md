# 任务执行模块（task_exec）

**路由前缀：** `/api/exec/`（HTTP API）、`/ws/task-logs/<task_id>/`（执行日志实时推送）  
**App Label：** `task_exec`

---

## 职责

**任务 = 任务流**：由多个**任务模块**（`TaskStep`）按序组成的可保存、可编辑、
可反复执行的任务定义。类型下沉到模块级，一个任务流可混合编排三种模块：

| 模块类型 | 说明 | 执行方式 |
|------|------|----------|
| `command` | Shell 命令 | ansible ad-hoc shell |
| `playbook` | 直接执行 `ANSIBLE_PROJECT_DIR` 内既有剧本 | ansible-playbook + 模块级 tags/extra-vars |
| `method` | 平台方法（仅承载运维后台能力：剧本+平台管理的 tags、云主机管理等） | 委托 `method_mgr` 对应 provider 执行器 |
| `pause` | 暂停确认：任务流停在该模块等待人工继续/中止，timeout 为等待上限（超时自动中止） | runner 置任务 `paused` 并轮询 `pause_action`，确认接口写 continue/abort |

**执行语义**：模块间按 order 顺序执行，任一模块失败即中止后续（标记
skipped）；模块内主机按任务级 exec_mode 并发/串行（串行 = `--forks 1`）。

**主机范围**：任务级项目组/渠道/环境为全局默认，**三维度必选**（不支持
留空=不限，未选齐不予保存与执行）；模块可 `use_custom_scope` 自定义三维度
独立匹配（同样三维度必选）。执行时实时匹配可执行主机（仅已规划服务的
主机）：全局范围主机默认全选、可勾除；自定义范围模块按其维度全量执行。

**逐模块勾选**：执行表单不再共用一张全局主机表，**每个「跟随全局」的模块
各有一张候选主机表**（候选 = 全局匹配集，按模块 `service_type` 收窄），各自
勾选、各自筛选（主机名/IP + 服务类型），勾选按主机 id 维护，切筛选不丢。
自定义范围模块按维度全量执行、只读展示；暂停模块不涉及主机。

`execute` body：`step_servers = {模块id: [主机id]}` 为准，`server_ids`
作为未单独勾选模块的兜底与任务级快照来源。模块勾选落库到
`TaskStep.servers`（最近一次执行快照），`runner._step_servers` 优先取它，
为空回退 `task.servers`（兼容存量/外部调用）；`task.servers` 存各模块并集。

**默认全选开关**（`ExecutionTask.default_select_all`，默认 True）：关闭后
执行表单每个模块都不预勾选，必须逐模块人工确认；`execute` 缺省
`server_ids` 且模块未出现在 `step_servers` 时直接拒绝（点名未勾选的模块）。

**模块服务类型**（`TaskStep.service_type`，非暂停模块**必选**）：与 `use_custom_scope`
**正交**，对「跟随全局」与「自定义」两种范围都生效——在该范围解析出的主机上
再收窄到**已规划/已部署该服务类型**的主机。同一任务流据此对不同服务类型跑
不同模块（如模块 1 只在网关机执行、模块 2 只在游戏服执行），无需为每个模块
重选三维度。暂停确认模块不涉及主机，不允许选服务类型；其余模块保存时必选
（不支持「不限」）。字段仍可空只为兼容 pause 与 0015 之前的存量数据——存量
模块视为不限，执行不拦，编辑保存时会被要求补选。

服务类型与服务状态必须命中**同一条** `ServerService` 记录（合并进单次
`filter`）：分开 `filter` 会各自 JOIN，「A 已规划 + B 已下线」的主机会被
`service_type=B` 误判为可执行。`asset_mgr` 的 `service_type` /
`service_status` / `executable=true` 三个查询参数同理已合并。

**执行变量**（`ExecVariable`）：变量绑定**项目组/渠道/环境生效条件**
（三维度必选），执行时仅加载与**模块有效维度**（自定义范围用模块维度，
否则任务全局维度）精确匹配的启用变量；变量值为 **YAML 文本**（保存时
校验），解析后与 CD 制品变量合并落临时工作目录 `vars.yml`，以
`--extra-vars @vars.yml` 注入三类模块（命令内可用 Jinja `{{ var }}` /
`{{ var.子键 }}` 引用），模块自身的额外变量作为后一个 `-e` 传入
（ansible 同名覆盖）。

走文件而非命令行 JSON 的原因：变量值不进 argv（同机 `ps` 可见）、不进
`common.ansible` 的命令行日志，且事后可原样复制复现。**仍是 extra vars，
ansible 优先级最高**——剧本内 `vars:` 不会反过来覆盖平台变量，与改造前语义
一致（若要让剧本可覆盖，才需要改用 inventory group_vars，本期不做）。
每次保存/回滚落一条**版本快照**
（`ExecVariableVersion`，`EXEC_VARIABLE_VERSION_LIMIT` 默认保留 10 版），
支持回滚。

**机读通道**：`resolved` 动作单独挂了 API 凭证认证（`variable:read` 作用域，
`include_secrets=true` 另需 `variable:secret`），并按凭证数据范围校验维度。只在这个
动作上开——脚本要的是「按维度解析好的变量字典」，不是整张变量表，把 list 也开出去
只是白扩大暴露面。

**对外取值**（`GET /api/exec/variables/resolved/`）：供 Jenkins 等外部系统按
维度拉取**已解析合并**的变量字典，配合服务器接口（`executable=true`）自行
拼装 ansible 调用。与执行器共用 `runner.collect_dim_variables()`，保证内外
注入语义一致（一条记录 = 一个 ansible 变量，值为 YAML 解析结果，空值为
`''`）。三维度必填（缺任一个 400，避免精确匹配语义含糊），取值纯数字按 id、
否则按 code；`include_artifact=true` 追加该维度制品变量 `package_url` /
`package_version` / `package_md5`（`artifact_version` 可锁定版本，不存在则
400）；`output=yaml` 返回**不带 `{code,msg,data}` 包装**的纯 YAML，可直接：

```bash
curl -sH "Authorization: Bearer $TOKEN" \
  "$BASE/api/exec/variables/resolved/?project_group=p01&channel=qixia&env=dev&output=yaml" \
  -o vars.yml
ansible-playbook site.yml -e @vars.yml
```

参数名用 `output` 而非 `format`：后者被 DRF 内容协商（`URL_FORMAT_OVERRIDE`）
占用，传 `format=yaml` 会 404。变量不做跨记录 flatten（顶层键合并会因同名互相
覆盖且与执行器行为不一致），需要顶层变量就按 `key` 建独立记录。

### 敏感变量（`ExecVariable.is_secret`）

变量里确实会放密码。而这张表原来是明文存、列表接口对**所有登录用户只读**——
等于任何账号 `GET /api/exec/variables/` 就能把全部密码拿走。标记为敏感后：

| 环节 | 行为 |
|------|------|
| 存储 | `value` 存 Fernet 密文（`common/crypto.py`），只有 `get_value()` 能还原 |
| 读接口 | 列表/详情/**历史版本**一律不回显值，给 `has_value` 布尔 |
| 看真值 | `POST /api/exec/variables/{id}/reveal/`，**管理员 + sudo 二次验证**；用 POST 而非 GET 正是为了让它进审计的写操作记录 |
| 注入 ansible | **明文**，剧本无需任何改动——这是「加密后还得能用」的落点 |
| 值语义 | 按**字面字符串**注入，不过 YAML 解析。密码里的 `#` 会被 YAML 当注释截断、`k: v` 会被解析成字典，这类静默改值比报错难查得多 |
| 执行输出 | 已知明文在进库/落盘/WebSocket 前替换为 `***`（`live.mask_secrets`）|
| 现场归档 | `vars.yml` 与 `command.txt` 打码后落盘（归档保留期比一次执行长得多）|
| 对外 resolved | **默认排除**；`include_secrets=true` 仅管理员可用，响应里 `secret_keys_excluded` 告知被排除的变量名 |
| 审计请求体 | `/api/exec/variables/` 的 `value` 一律打码（`audit.middleware.PATH_SENSITIVE_KEYS`）|

打码为什么放在输出汇聚层而不是靠剧本写 `no_log`：剧本不是平台写的，指望每个
作者都记得加不现实。`LiveSink.feed` 是所有执行输出的唯一收口，在那里过滤是唯一
不依赖别人配合的位置。

**已接受的局限**：分片喂入时，明文正好被切在两个分片之间会漏过实时那一层。但
模块结束时 runner 会对完整输出整体再打一遍，且实时块在 `end()` 时被丢弃换成完整
输出——落库/落盘的最终内容一定是干净的，漏的只是发起人自己正在看实时日志的
那一瞬间。

切换 `is_secret` 时值会重新编码（明文→加密 / 密文→解开）：只改标记不改内容会
留下「标记说是明文、内容其实是密文」的变量，之后注入给 ansible 的就是
`gAAAAA...` 那串东西。

**保存与执行分离**：新建/编辑仅保存定义（嵌套模块整体替换）；执行为独立
动作，每次执行落一条执行日志（含全局主机快照与逐模块结果 steps_result），
任务本体字段仅为「最近一次执行」快照。

---

## 文件结构

```
apps/task_exec/
├── models.py        # ExecVariable（执行变量，支持 is_secret 加密）/ Credential（密钥加密存储）/ ExecutionTask（任务流）
│                    #   / TaskStep（任务模块）/ TaskExecutionLog
│                    #   match_executable_servers：全局/模块维度匹配共用口径
├── serializers.py   # 变量 key 校验、凭据脱敏、嵌套模块整体读写与内容校验、方法 service_type 执行校验
├── views.py         # 变量/凭据/任务 ViewSet + matched_servers/execute/output/logs 动作 + 剧本列表
├── trigger.py       # 发起前置校验 + 主机快照固化 + 调度（页面执行与接口触发共用）
├── webhook.py       # TaskTriggerView：API 凭证鉴权的机读触发入口
├── log_files.py     # 日志文件浏览：TASK_LOG_DIR 只读目录树 / 文本分段读 / 原文件下载（仅管理员）
├── runner.py        # 任务流执行：逐模块解析主机 + 动态 inventory + 变量注入 + ansible 调用，失败中止
├── live.py          # 实时输出汇聚 + 敏感变量打码（mask_secrets / LiveSink.add_secrets）
├── live.py          # LiveSink：流式日志汇聚（节流写库预览 + 落盘 + WebSocket 推送）
├── consumers.py     # TaskLogConsumer：/ws/task-logs/<task_id>/ 日志推送（子协议 JWT 鉴权）
├── routing.py       # WebSocket 路由（ops_manager/asgi.py 挂载）
├── tasks.py         # dispatch：thread/celery/sync 三种后端（日志落主机快照与模块结果）
└── management/      # clean_task_logs 日志清理命令
```

---

## 流式执行日志（边跑边出）

ansible 输出不再等模块跑完才可见：`common.ansible.run_stream` 以
**伪终端（pty）**启动 ansible（posix），子进程视输出为终端 → 行级 flush
且 `force_color` 生效，读到的分片经 `on_data` 回调交给 `live.LiveSink`：

- **实时预览**：分片节流（0.5s 或 2KB）写 `ExecutionTask.output` 与
  `TaskExecutionLog.output`，存尾部 60000 字符（超出加截断标注）：既是
  WebSocket 连接时的快照来源，也是轮询回退（`output?offset=`，1s）的数据源；
- **完整落盘**：每次执行流式写 `TASK_LOG_DIR/<task_id>/<log_id>.log`
  （0600，父目录 0700），**所有执行都落盘**（旧行为仅超长才落盘），执行
  记录详情接口读该文件返回全量。落盘以「已完成模块」为粒度追加（前缀式 append，
  天然去重，并行组不会交错），实时性由写库预览承担；
- **超时**：`threading.Timer` 到点按**进程组**终止（SIGTERM → 3s → SIGKILL），
  ansible 派生的 ssh 子进程一并回收，返回 rc=124；
- **颜色**：日志内保留 ANSI 颜色码，前端 `utils/ansi.js` 转成 span 着色
  （`TASK_LOG_COLOR=false` 可关，此时 ansible 输出纯文本）；
- **agent 通道**：`agent_mgr.dispatch.run_via_agent` 轮询 `AgentJob.output`
  时把新增分片喂给同一 sink，NAT 主机执行同样实时可见；
- 方法执行器（`ansible_playbook` provider）透传 `on_data`；Jenkins/JumpServer
  等非 ansible provider 忽略该参数，仍为整段输出。

### 实时通道：WebSocket 推送（轮询为回退）

`/ws/task-logs/<task_id>/`（`consumers.TaskLogConsumer`，路由
`task_exec/routing.py`，与 agent 通道一起挂在 `ops_manager/asgi.py`）：

- **鉴权**：token 走 **子协议** `['jwt', <access_token>]`（不进 URL 与 nginx
  访问日志），兼容 `?token=`；校验到有效启用用户即放行（任务详情登录可读）。
  失败关 4401，任务不存在关 4404。
- **消息**：连接先收 `{"kind":"full", output, offset, status, ...}` 快照，
  之后收 `{"kind":"delta", output}` 增量与 `{"kind":"status", ...}` 状态变化
  （running / paused / 终态，来自 `live.push_status`）。客户端可发
  `{"type":"ping"}`，服务端回 `{"kind":"pong"}`。
- **增量语义**：sink 以 render 全文长度为基准发增量；临时提示（失败等待）、
  并行组多块交错、模块结束交还 outputs 时改发整段（`kind=full`，尾部预览），
  客户端按 kind 决定追加还是替换。
- **回退**：连接建立时先 `group_add` 探测 channel layer，未配/连不上 Redis
  直接拒握手（4503）；`live.push` 失败只告警并停止后续推送。前端
  `utils/taskLogStream.js` 在握手失败、连接中断、或 60s 收不到任何帧（含
  pong）时回退到 `output?offset=` 轮询，功能等价。
- **部署**：执行进程（WSGI/celery worker）与 WebSocket 进程（daphne）不同
  进程，跨进程推送**依赖 Redis channel layer**（`CHANNEL_LAYERS`）；nginx
  `/ws/` 已反代到 daphne 8001（见 `deploy/nginx.conf`）。dev 的 runserver
  自带 ASGI，同端口即可，vite 已代理 `/ws`。

### 执行现场归档（debug 复现）

临时工作目录执行完即删（含 inventory 与临时私钥），事后无从复现，故每个
模块执行时把现场归档到执行记录目录：

```
TASK_LOG_DIR/<task_id>/<log_id>.d/step<order>[-retry<n>]/
├── direct/                 # 直连组（agent 组为 agent-<agent_id>/）
│   ├── inventory.ini       # 无密钥版本
│   ├── vars.yml            # 变量管理 + 制品变量
│   └── command.txt         # 实际 argv，路径已改写为归档副本，复制即可重跑
```

- inventory 一律落**无密钥**版本（不写密码与私钥路径），复现时自行补凭据，
  `command.txt` 首行注释也写明这点；剧本模块另带 `# cwd:` 注释；
- 归档目录 0700、文件 0600；归档失败只告警，不影响执行本体；
- 重试单独落 `-retry<n>` 目录，每次尝试的现场都留痕；
- 直接调 `runner.run`（无执行记录，如单测）不归档；
- 读取走 `logs/{log_id}/scene/`（**仅管理员**，归档含变量值），前端执行日志页
  `TaskLog.vue` 的「执行现场」按钮按文件浏览内容；`file` 参数经 `resolve_within`
  限制在本条执行记录的归档目录内，防路径穿越。

### 日志文件浏览（按目录翻）

`scene/` 接口按「某任务的某次执行」定向取归档；另有一组不依赖数据库记录的
目录浏览接口（`log_files.py`），把 `TASK_LOG_DIR` 整棵树开放给前端页面
「任务执行 → 日志文件」（`views/task/LogFiles.vue`，仅管理员）：

- `GET /api/exec/log-files/?path=` 列目录，返回
  `{path, parent, entries:[{name,path,type,size,modified_at,label}], root}`；
  排序为目录优先、纯数字名（任务 id / 执行记录 id）按数值倒序（新的在前）；
  `label` 是可读性标注——根层取任务名，任务目录内取该次执行的状态与开始时间，
  记录已删则标注「记录已删除」，不影响浏览；
- `GET /api/exec/log-files/content/?path=&offset=&limit=&tail=true` 读文本，
  单次上限 512KB（`CONTENT_LIMIT`），`tail=true` 直接读末尾，返回 `eof` 供
  前端「继续加载」翻页；二进制文件（含 `\x00`）拒绝，提示下载；
- `GET /api/exec/log-files/download/?path=` 原文件流式下载（不受上限限制），
  下载名把目录层级拼进文件名，避免多个 `vars.yml` 互相覆盖。

三个接口的 `path` 均经 `resolve_within` 收敛到 `TASK_LOG_DIR` 内（realpath
比较，`../` 与指向外部的软链接一并拒绝），**只读**，无写/删接口；权限
`IsAdmin`（归档含变量值）。记录已被清理但文件仍在时同样能翻到。

日志保留（`clean_task_logs`，建议 cron 每日）：落盘文件按 mtime 保留
`TASK_LOG_FILE_RETENTION_DAYS`（默认 30 天，含记录已删的孤儿文件；清理后
对应记录 `log_file` 置空，详情页退回数据库预览），**执行现场归档目录
`<log_id>.d/` 同保留期一并清理**（目录 mtime 取内部文件最新值），执行日志
记录保留 `TASK_LOG_RETENTION_DAYS`（默认 90 天，删记录时带走其归档目录）。
`--files-only` 只清文件、`--dry-run` 只统计。

```bash
# crontab：每日 03:10 清理
10 3 * * * cd /opt/devops-manager && venv/bin/python manage.py clean_task_logs >> logs/clean_task_logs.log 2>&1
```

---

## 动态 inventory（三类任务共用）

`runner._inventory_content` 基于任务所选主机生成临时 INI inventory
（方法任务由 runner 生成后以 `inventory_path` 传给 provider 执行器，同时传 `servers`（模块匹配到的 Server 列表）供不走 Ansible 的 provider——如 `jumpserver` 注册——直接消费主机对象）：

- **主机名作 inventory 别名**：ansible 日志按主机名（而非 IP）显示各主机执行结果；
  名称中的非法字符净化为 `_`，空名回退 IP，别名撞车（不同名净化后同名）追加 IP 保证唯一
- **连接地址公网优先**（`Server.conn_ip` 模型属性）：`ansible_host` 有公网 IP 用公网，
  NAT 出局主机（无公网 IP）回退私网；JumpServer 注册地址、执行日志主机快照、
  前端主机选择/分配页展示均走同一口径
- **服务类型注入**（数据源：主机服务规划 `asset_mgr.ServerService`，取
  planned/deployed 记录，offline 不算）：
  - 主机变量 `service_types="<code1>,<code2>"`，剧本/命令中可引用
  - 按服务类型 code 生成分组 `[<code>]`，`pattern` 字段可直接圈选（如 `game-server`）

示例：

```ini
game-01 ansible_host=10.0.0.8 ansible_port=22 ansible_user=root service_types="game-server,redis"

[game-server]
game-01

[redis]
game-01
```

---

## Ansible 项目目录（playbook / 方法任务）

playbook 任务与 `ansible_playbook` provider 的方法任务都在集中管理的
Ansible 项目目录内执行剧本：

- 目录位置由 `ANSIBLE_PROJECT_DIR` 配置（默认仓库根 `ansible/`，已含示例工程：
  `site.yml` 入口剧本 + `common_check` 角色，约定见 `ansible/README.md`）
- **主机与 inventory 不再由该目录维护**：执行主机来自任务所选服务器，
  平台动态生成 inventory（含服务类型分组，剧本内可用 `service_types` 变量
  与 `[<服务 code>]` 分组）
- playbook 模块：**直接选「方法管理」登记的 Ansible 剧本方法**（provider =
  `ansible_playbook` 区分剧本类与其他平台方法），剧本路径与 tags 候选均
  取自方法 `config`，不再扫描目录匹配（后端 playbook/tags 字段仍为自由
  字符串；`GET /api/exec/playbooks/` 保留供方法管理登记时参考）；
  **tags 单选且必填**（不支持留空执行全部）；
  平台方法模块则只列其他 provider（如云主机管理），无 tags/额外变量，
  改为模块级 JSON 参数框（`TaskStep.params`，预留给 provider 消费）
- 方法任务：**一个平台方法 = 一个剧本 + 一组 tags**（`Method.config.playbook/tags`），
  任务级 `tags` / `limit` 覆盖方法默认值
- 剧本路径按项目目录内相对路径解析，禁止越出目录
  （`common.ansible.resolve_within` 路径穿越校验）
- 注意：角色级 tags 会附加到角色内全部任务，入口剧本只在任务级挂 tags

---

## 执行权限与执行时校验

**未在「服务器分配」中规划服务类型的主机不具备执行条件**，主机相关校验
全部在执行入口（保存任务不校验主机）：

| 层级 | 时机 | 规则 |
|------|------|------|
| `match_executable_servers` | 执行表单/执行入口 | 只匹配已规划/已部署服务的主机（offline 不算），按维度过滤（留空=不限），可再按服务类型收窄；任务全局与模块自定义共用口径 |
| `TaskStep.resolve_servers` | 执行入口 / runner 每模块 | 模块最终主机：自定义范围实时匹配、否则取全局范围勾选主机，两者再按模块 `service_type` 过滤（`filter_by_service_type`） |
| `execute` 动作校验 | 发起执行 | `step_servers`/`server_ids` 仅作用于全局范围模块，模块 id 必须属于本任务、主机必须在全局匹配集内；关闭 `default_select_all` 时未勾选的模块直接拒绝；逐模块解析主机后必须 ≥1 台；执行中禁止再次发起 |
| `validate_service_planned` | 发起执行（方法模块，逐个） | 方法关联了服务类型时，该模块的目标主机必须已规划/已部署**该**服务（部署前置校验） |
| `runner._guard` | 每个模块执行前 | 兜底：执行排队期间规划被撤销（服务全部下线）时该模块失败，不生成 inventory |

**执行方式**：`exec_mode = parallel`（并发，ansible 默认并发数）/
`serial`（串行，命令行加 `--forks 1` 逐台执行），任务级统一、全部模块生效。

前端流程：新建/编辑（模块卡片编排，保存定义）→ 列表「执行」→ 执行表单
（任务流预览 + 全局范围主机默认全选、可勾除）→ 确认框（任务名称/维度/
逐模块流程，生产环境标红）→ 自动定位任务列表下方的执行日志面板，日志实时
推送（WebSocket，不可用自动回退轮询）直至完成。

---

## 模块间并行（run_parallel）

- 模块默认按序串行；`TaskStep.run_parallel=True` 且相邻的模块合并为一个
  **并行组**（`runner._build_stages`），组内模块线程并发执行、互不打断，
  全部完成后按模块顺序拼接输出，失败进入失败处理（重试仅重跑组内失败模块）。
- pause 模块禁止标记并行（序列化器校验），强制独立成阶段。
- 与任务级 `exec_mode` 正交：exec_mode 管模块内主机并发（--forks），
  run_parallel 管模块之间并发。
- steps_result 每项新增 `parallel` 布尔，前端日志树以 ∥ 标记。

## 暂停确认（pause 模块）

- 执行到 pause 模块：任务状态置 `paused`（`pause_type=confirm`），已完成模块
  输出与暂停提示由 sink 强制落库并推送（见「流式执行日志」），前端即时可见。
- 确认：`POST /api/exec/tasks/{id}/confirm/` `{"action": "continue"|"abort"}`
  （仅管理员、仅 paused 状态可操作）；动作写入 `ExecutionTask.pause_action`，
  runner 每 2s 轮询消费后清空并恢复 `running`。
- abort → 模块 rc=1，后续模块 skipped；超时未确认 → rc=124 中止。
  pause 模块的中止/超时直接终止任务流，**不进入失败处理**。
- paused 状态下任务禁止编辑与重复发起。

## 模块失败处理（重试 / 跳过 / 中止）

- 模块失败（rc != 0）后任务流不再立即中止：任务状态置 `paused`
  （`pause_type=failed`），等待人工经 confirm 接口处理
  `{"action": "retry"|"skip"|"abort"}`（仅管理员）：
  - `retry`：重跑当前阶段的失败模块（并行组只重跑失败成员，序号沿用），
    可反复重试；模块头追加 ` · 重试#N` 标记，steps_result 该模块
    `attempts` 递增（日志树右侧显示已重试次数）。
  - `skip`：跳过失败模块继续后续阶段；该模块 steps_result 标记
    `skipped_over: true`（前端显示「失败·已跳过」），任务最终仍记失败
    （return_code = 首个失败模块 rc）。
  - `abort`：中止，后续模块标记 skipped。
- 等待超时按中止处理。超时时长：系统配置 `task.failure_wait_timeout` 优先，
  其次 settings `TASK_FAILURE_WAIT_TIMEOUT`（环境变量，默认 600s）；
  **配置为 0 = 不等待，恢复「失败即中止」旧行为**。
- confirm 接口按 `pause_type` 校验动作：confirm 态仅收 continue/abort，
  failed 态仅收 retry/skip/abort（`pause_type` 空值兼容存量，视为 confirm）。
- `output` 轮询接口与任务序列化器均返回 `pause_type`，前端据此切换
  暂停操作条（继续/中止 vs 重试/跳过/中止）。

## 接口

```
GET/POST/DELETE     /api/exec/credentials/        凭据 CRUD（仅管理员；密钥/密码只写不回显）
GET/POST/PUT/PATCH/DELETE /api/exec/variables/    执行变量 CRUD（普通用户只读；支持 project_group/channel/env 筛选）
GET                 /api/exec/variables/resolved/ 按维度取已解析变量字典（外部系统喂 ansible）
                                                  必填 project_group/channel/env（id 或 code）；
                                                  可选 include_artifact=true[&artifact_version=x]、output=yaml
GET                 /api/exec/variables/{id}/versions/  历史版本列表（默认保留 10 版）
POST                /api/exec/variables/{id}/rollback/  回滚到指定版本 body {version}（仅管理员，回滚亦落新版本）
GET                 /api/exec/playbooks/          剧本列表（仅管理员；ANSIBLE_PROJECT_DIR 内相对路径）
GET/POST/PUT/PATCH/DELETE /api/exec/tasks/        任务流 CRUD（steps 嵌套整体读写；保存不执行；执行中禁止编辑）
GET                 /api/exec/tasks/{id}/matched_servers/  执行表单数据源：{global: {...}, steps: [{order,name,custom,total,servers?}]}
POST                /api/exec/tasks/{id}/execute/ 发起执行，body {server_ids:[...]} 仅作用于全局范围模块（缺省=全部全局匹配主机）
POST                /api/exec/tasks/{id}/trigger/ 接口/Webhook 触发（API 凭证 task:trigger，免登录）：
                                                  仅接受 default_select_all=True 的任务（接口无法逐模块勾选主机）；
                                                  生产环境任务须带 body {confirm_production:true}；
                                                  主机范围恒为「维度匹配到的全部主机」，日志 trigger_type='api'
POST                /api/exec/tasks/{id}/confirm/ 人工处理暂停：确认模块 continue/abort；失败等待 retry/skip/abort（仅管理员）
GET                 /api/exec/tasks/{id}/output/  轻量轮询：状态与输出；?offset=N 只回增量（delta=true 时前端追加，返回新 offset）
GET                 /api/exec/tasks/{id}/logs/    执行历史列表（含主机数/执行方式/逐模块结果 steps_result）
GET                 /api/exec/tasks/{id}/logs/{log_id}/  单条日志详情（完整输出 + 主机快照）
GET                 /api/exec/tasks/{id}/logs/{log_id}/scene/  执行现场归档（仅管理员）：
                                                  缺省列清单 {files:[{path,size}],retained}；
                                                  ?file=step1/direct/vars.yml 返回 {path,content,truncated}
GET                 /api/exec/log-files/          日志目录浏览（仅管理员）：?path= 相对 TASK_LOG_DIR，缺省为根
GET                 /api/exec/log-files/content/  日志文本读取（仅管理员）：?path= 必填，?offset=&limit=&tail=true 分段读
GET                 /api/exec/log-files/download/ 日志原文件下载（仅管理员）：?path= 必填
```

任务读写权限：`module_perm('exec')` 按方法推档（读 read、保存/编辑 write）；
发起执行另看能力位 `can_execute` / `can_exec_prod`。方法库（`method`）与执行变量
（`variable`）是**另外两个模块**：能改任务流不等于能改方法模板（模板里能写任意
命令）或读写敏感变量。
列表 `task_type` 筛选语义 = 包含该类型模块的任务（`steps__task_type`）。

### 保存校验规则（仅内容字段）

| 规则 | 说明 |
|------|------|
| 任务流 | `steps` 至少 1 个模块；编辑时整体替换、序号按提交顺序归一 |
| 命令模块 | `command` 必填 |
| playbook 模块 | `playbook` 必填；`tags` 必填且单选一个（不支持留空执行全部） |
| 方法模块 | `method` 必填且启用；`params` 须为 JSON 对象（预留给 provider 消费），无 tags/额外变量 |
| 主机范围 | 任务全局三维度**必选**；模块 `use_custom_scope` 时自定义三维度同样必选 |
| 模块服务类型 | 非 `pause` 模块**必选**（不支持不限），跟随全局/自定义范围均生效；`pause` 模块不允许选；存量空值仅执行时按不限兼容 |
| 执行方式 | `exec_mode` 默认 parallel |
| 默认全选主机 | `default_select_all` 默认 True；False 时执行必须显式传 `server_ids` |
| 变量 | `key` 须为合法 ansible 变量名；`value` 须为合法 YAML；生效条件三维度必选 |
| 超时 | 模块级 `timeout` 秒，默认 300 |

主机相关校验见上节「执行时校验」。存量数据：0006 迁移将旧单动作任务
自动平移为单模块任务流。
