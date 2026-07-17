# 任务执行模块（task_exec）

**路由前缀：** `/api/exec/`  
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

**执行变量**（`ExecVariable`）：变量绑定**项目组/渠道/环境生效条件**
（三维度必选），执行时仅加载与**模块有效维度**（自定义范围用模块维度，
否则任务全局维度）精确匹配的启用变量；变量值为 **YAML 文本**（保存时
校验），解析后以 `--extra-vars` JSON 注入三类模块（命令内可用 Jinja
`{{ var }}` / `{{ var.子键 }}` 引用），模块自身的额外变量作为后一个
`-e` 传入（ansible 同名覆盖）。每次保存/回滚落一条**版本快照**
（`ExecVariableVersion`，`EXEC_VARIABLE_VERSION_LIMIT` 默认保留 10 版），
支持回滚。变量列表 API 亦可供 Jenkins 等外部系统拉取，配合服务器接口
（`executable=true`）自行拼装 ansible 调用（规划中）。

**保存与执行分离**：新建/编辑仅保存定义（嵌套模块整体替换）；执行为独立
动作，每次执行落一条执行日志（含全局主机快照与逐模块结果 steps_result），
任务本体字段仅为「最近一次执行」快照。

---

## 文件结构

```
apps/task_exec/
├── models.py        # ExecVariable（执行变量）/ Credential / ExecutionTask（任务流）
│                    #   / TaskStep（任务模块）/ TaskExecutionLog
│                    #   match_executable_servers：全局/模块维度匹配共用口径
├── serializers.py   # 变量 key 校验、凭据脱敏、嵌套模块整体读写与内容校验、方法 service_type 执行校验
├── views.py         # 变量/凭据/任务 ViewSet + matched_servers/execute/output/logs 动作 + 剧本列表
├── runner.py        # 任务流执行：逐模块解析主机 + 动态 inventory + 变量注入 + ansible 调用，失败中止
├── tasks.py         # dispatch：thread/celery/sync 三种后端（日志落主机快照与模块结果）
└── management/      # clean_task_logs 日志清理命令
```

---

## 动态 inventory（三类任务共用）

`runner._inventory_content` 基于任务所选主机生成临时 INI inventory
（方法任务由 runner 生成后以 `inventory_path` 传给 provider 执行器，同时传 `servers`（模块匹配到的 Server 列表）供不走 Ansible 的 provider——如 `jumpserver` 注册——直接消费主机对象）：

- **主机名作 inventory 别名**：ansible 日志按主机名（而非 IP）显示各主机执行结果；
  名称中的非法字符净化为 `_`，空名回退 IP，重名主机追加 IP 保证别名唯一
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
| `match_executable_servers` | 执行表单/执行入口 | 只匹配已规划/已部署服务的主机（offline 不算），按维度过滤（留空=不限）；任务全局与模块自定义共用口径 |
| `execute` 动作校验 | 发起执行 | `server_ids` 仅作用于全局范围模块，必须在全局匹配集内；逐模块解析主机后必须 ≥1 台；执行中禁止再次发起 |
| `validate_service_planned` | 发起执行（方法模块，逐个） | 方法关联了服务类型时，该模块的目标主机必须已规划/已部署**该**服务（部署前置校验） |
| `runner._guard` | 每个模块执行前 | 兜底：执行排队期间规划被撤销（服务全部下线）时该模块失败，不生成 inventory |

**执行方式**：`exec_mode = parallel`（并发，ansible 默认并发数）/
`serial`（串行，命令行加 `--forks 1` 逐台执行），任务级统一、全部模块生效。

前端流程：新建/编辑（模块卡片编排，保存定义）→ 列表「执行」→ 执行表单
（任务流预览 + 全局范围主机默认全选、可勾除）→ 确认框（任务名称/维度/
逐模块流程，生产环境标红）→ 自动定位任务列表下方的执行日志面板，轮询至完成。

---

## 模块间并行（run_parallel）

- 模块默认按序串行；`TaskStep.run_parallel=True` 且相邻的模块合并为一个
  **并行组**（`runner._build_stages`），组内模块线程并发执行、互不打断，
  全部完成后按模块顺序拼接输出，任一失败则后续阶段整体 skipped。
- pause 模块禁止标记并行（序列化器校验），强制独立成阶段。
- 与任务级 `exec_mode` 正交：exec_mode 管模块内主机并发（--forks），
  run_parallel 管模块之间并发。
- steps_result 每项新增 `parallel` 布尔，前端日志树以 ∥ 标记。

## 暂停确认（pause 模块）

- 执行到 pause 模块：任务状态置 `paused`，把已完成模块输出实时落库
  （runner 每模块结束也会持久化进度），前端轮询可见暂停提示。
- 确认：`POST /api/exec/tasks/{id}/confirm/` `{"action": "continue"|"abort"}`
  （仅管理员、仅 paused 状态可操作）；动作写入 `ExecutionTask.pause_action`，
  runner 每 2s 轮询消费后清空并恢复 `running`。
- abort → 模块 rc=1，后续模块 skipped；超时未确认 → rc=124 中止。
- paused 状态下任务禁止编辑与重复发起。

## 接口

```
GET/POST/DELETE     /api/exec/credentials/        凭据 CRUD（仅管理员；密钥/密码只写不回显）
GET/POST/PUT/PATCH/DELETE /api/exec/variables/    执行变量 CRUD（普通用户只读；支持 project_group/channel/env 筛选）
GET                 /api/exec/variables/{id}/versions/  历史版本列表（默认保留 10 版）
POST                /api/exec/variables/{id}/rollback/  回滚到指定版本 body {version}（仅管理员，回滚亦落新版本）
GET                 /api/exec/playbooks/          剧本列表（仅管理员；ANSIBLE_PROJECT_DIR 内相对路径）
GET/POST/PUT/PATCH/DELETE /api/exec/tasks/        任务流 CRUD（steps 嵌套整体读写；保存不执行；执行中禁止编辑）
GET                 /api/exec/tasks/{id}/matched_servers/  执行表单数据源：{global: {...}, steps: [{order,name,custom,total,servers?}]}
POST                /api/exec/tasks/{id}/execute/ 发起执行，body {server_ids:[...]} 仅作用于全局范围模块（缺省=全部全局匹配主机）
GET                 /api/exec/tasks/{id}/output/  轻量轮询：状态与输出
GET                 /api/exec/tasks/{id}/logs/    执行历史列表（含主机数/执行方式/逐模块结果 steps_result）
GET                 /api/exec/tasks/{id}/logs/{log_id}/  单条日志详情（完整输出 + 主机快照）
```

任务读写权限：普通用户只读，管理员可保存/编辑/执行（`IsAdminOrReadOnly`）。
列表 `task_type` 筛选语义 = 包含该类型模块的任务（`steps__task_type`）。

### 保存校验规则（仅内容字段）

| 规则 | 说明 |
|------|------|
| 任务流 | `steps` 至少 1 个模块；编辑时整体替换、序号按提交顺序归一 |
| 命令模块 | `command` 必填 |
| playbook 模块 | `playbook` 必填；`tags` 必填且单选一个（不支持留空执行全部） |
| 方法模块 | `method` 必填且启用；`params` 须为 JSON 对象（预留给 provider 消费），无 tags/额外变量 |
| 主机范围 | 任务全局三维度**必选**；模块 `use_custom_scope` 时自定义三维度同样必选 |
| 执行方式 | `exec_mode` 默认 parallel |
| 变量 | `key` 须为合法 ansible 变量名；`value` 须为合法 YAML；生效条件三维度必选 |
| 超时 | 模块级 `timeout` 秒，默认 300 |

主机相关校验见上节「执行时校验」。存量数据：0006 迁移将旧单动作任务
自动平移为单模块任务流。
