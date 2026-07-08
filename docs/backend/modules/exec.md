# 任务执行模块（task_exec）

**路由前缀：** `/api/exec/`  
**App Label：** `task_exec`

---

## 职责

统一的任务执行入口，三种任务类型：

| 类型 | 说明 | 执行方式 |
|------|------|----------|
| `command` | Shell 命令 | ansible ad-hoc shell |
| `playbook` | 直接执行 `ANSIBLE_PROJECT_DIR` 内既有剧本 | ansible-playbook + 任务级 tags/limit/extra-vars |
| `method` | 平台方法（仅承载运维后台能力：剧本+平台管理的 tags、云主机管理等） | 委托 `method_mgr` 对应 provider 执行器 |

**三类任务的执行主机统一来自平台服务器台账**（前端按项目组/渠道/环境筛选、
仅可选已规划服务的主机），执行时基于所选主机动态生成 inventory。
同时管理连接凭据与执行日志（每次执行落一条日志，重跑追加）。

---

## 文件结构

```
apps/task_exec/
├── models.py        # Credential / ExecutionTask / TaskExecutionLog
├── serializers.py   # 凭据脱敏、任务校验（含服务规划前置校验）
├── views.py         # 凭据/任务 ViewSet + rerun/output/logs 动作
├── runner.py        # 执行分派：动态 inventory 生成 + ansible 调用
├── tasks.py         # dispatch：thread/celery/sync 三种后端
└── management/      # clean_task_logs 日志清理命令
```

---

## 动态 inventory（三类任务共用）

`runner._inventory_content` 基于任务所选主机生成临时 INI inventory
（方法任务由 runner 生成后以 `inventory_path` 传给 provider 执行器）：

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
- playbook 任务：剧本由 `GET /api/exec/playbooks/` 列表选择（扫描项目目录内
  `*.yml/*.yaml`，排除 roles/inventories/group_vars 等非剧本目录）
- 方法任务：**一个平台方法 = 一个剧本 + 一组 tags**（`Method.config.playbook/tags`），
  任务级 `tags` / `limit` 覆盖方法默认值
- 剧本路径按项目目录内相对路径解析，禁止越出目录
  （`common.ansible.resolve_within` 路径穿越校验）
- 注意：角色级 tags 会附加到角色内全部任务，入口剧本只在任务级挂 tags

---

## 执行权限：服务规划前置校验

**未在「服务器分配」中规划服务类型的主机不允许作为执行目标**，两层拦截：

| 层级 | 时机 | 规则 |
|------|------|------|
| `ExecutionTaskSerializer._validate_servers_planned` | 创建任务 | 所选主机必须有 ≥1 条 planned/deployed 服务记录，否则 400 并提示去「配置管理 → 服务器分配」 |
| `runner._prepare_servers` 守卫 | 每次执行（三类任务共用） | 兜底重跑场景：创建后规划被撤销（服务全部下线）时返回失败，不生成 inventory |
| `ExecutionTaskSerializer._validate_service_planned` | 创建方法任务 | 方法关联了服务类型时，目标主机必须已规划/已部署**该**服务（更严格的部署前置校验） |

前端任务创建页联动：未规划主机在目标主机选择器中禁用并标注「未规划服务」，
已规划主机在标签中展示其服务类型。

---

## 接口

```
GET/POST/DELETE /api/exec/credentials/        凭据 CRUD（仅管理员；密钥/密码只写不回显）
GET             /api/exec/playbooks/          剧本列表（仅管理员；ANSIBLE_PROJECT_DIR 内相对路径）
GET/POST/DELETE /api/exec/tasks/              任务列表/创建（创建即调度执行）/删除
POST            /api/exec/tasks/{id}/rerun/   重跑（追加执行日志）
GET             /api/exec/tasks/{id}/output/  轻量轮询：状态与输出
GET             /api/exec/tasks/{id}/logs/    执行历史列表（不含输出大字段）
GET             /api/exec/tasks/{id}/logs/{log_id}/  单条日志详情（超长输出从落盘文件读取）
```

任务读写权限：普通用户只读，管理员可创建/重跑（`IsAdminOrReadOnly`）。

### 任务创建校验规则

| 规则 | 说明 |
|------|------|
| 目标主机 | 三类任务均必选 `server_ids` 至少一台（执行主机统一来自平台台账） |
| 命令任务 | `command` 必填 |
| playbook 任务 | `playbook` 必填（项目目录内相对路径） |
| 方法任务 | `method` 必填且启用 |
| 服务规划 | 所选主机必须已规划服务（见上节） |
| 超时 | `timeout` 秒，默认 300 |
