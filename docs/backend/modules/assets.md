# 资产管理模块（asset_mgr）

**路由前缀：** `/api/assets/`  
**App Label：** `asset_mgr`

---

## 职责

CMDB 核心模块，包含服务器资产与五类基础数据字典：

- **服务器**：资产 CRUD，支持状态 / 环境 / 项目组 / 渠道 / 云厂商 / 服务类型 / 关键字过滤与统计
- **项目组**：游戏项目/团队维度（替代原自由文本"部门"字段）
- **渠道**：游戏发行/联运渠道（官服、华为、TapTap、App Store 等）
- **环境**：开发/测试/生产等，`is_prod` 标记生产环境
- **云厂商**：阿里云/腾讯云/AWS 等，自建机房也作为一种"厂商"维护
- **服务类型**：主机可承载的服务（游戏服/网关/DB/Nginx 等），通过 `ServerService`
  关联表记录每台主机的服务规划与部署状态（部署前置规划见 config 模块文档）

---

## 文件结构

```
apps/asset_mgr/
├── __init__.py
├── apps.py          # AppConfig
├── models.py        # DictBase 基类 + 四张字典表 + Server
├── serializers.py   # 字典序列化器 + ServerSerializer（含 *_name 展示字段）
├── views.py         # BaseDictViewSet + 各字典 ViewSet + ServerViewSet
├── urls.py          # Router 注册
└── tests.py         # 种子数据 / CRUD 权限 / 删除保护 / 筛选测试
```

---

## 模型设计

### 字典公共基类

```python
class DictBase(models.Model):
    """CMDB 字典表公共基类（abstract）。"""
    name       = CharField(max_length=100)              # 名称
    code       = SlugField(max_length=50, unique=True)  # 唯一编码
    enabled    = BooleanField(default=True)             # 启用开关
    sort_order = PositiveIntegerField(default=0)        # 排序
    remark     = TextField(blank=True)
    created_at / updated_at
```

### 四张字典表

| 模型 | 表名 | 差异字段 |
|------|------|------|
| `ProjectGroup` | `asset_project_group` | `owner` 负责人、`contact` 联系方式 |
| `Channel` | `asset_channel` | `platform`（all / android / ios）|
| `Environment` | `asset_environment` | `is_prod` 是否生产环境 |
| `CloudProvider` | `asset_cloud_provider` | — |
| `ServiceType` | `asset_service_type` | — |

### ServerService（主机-服务类型关联）

表 `asset_server_service`，`unique(server, service_type)`，记录三态服务生命周期：

- `planned` 已规划（可部署）→ `deployed` 已部署 → `offline` 已下线（保留追溯，不删记录）
- 合法流转由 `ServerService.STATUS_TRANSITIONS` 定义：`planned→deployed`、
  `deployed→offline|planned`（回退纠错）、`offline→planned`（重新规划）
- `planned_at` / `deployed_at` / `offline_at` 记录最近一次进入对应状态的时间
- `service_type` 外键 `PROTECT`；`server` 外键 `CASCADE`（主机删除时记录随之删除）
- **进程规划字段**：`process_name`（进程名，规划时自动生成）、`process_count`（进程数，默认 1）
  - 进程名格式 `<服务类型code>-<两位序号>`（如 `nginx-01`），序号在「相同云厂商+项目组+
    环境+渠道」的主机范围内按服务类型递增，与主机名中的序号无关；取消规划空出的序号可复用
  - 生成逻辑见 `ServerService.next_process_name` / `build_planned_links`（批量创建时
    组内预留防重）

### Server 关联

服务器通过可空外键引用四张字典表（`on_delete=PROTECT`，防止误删仍被引用的基础数据），
另有 `region` 文本字段记录地域/机房：

```python
project_group  = FK(ProjectGroup,  PROTECT, null=True, related_name='servers')
channel        = FK(Channel,       PROTECT, null=True, related_name='servers')
environment    = FK(Environment,   PROTECT, null=True, related_name='servers')
cloud_provider = FK(CloudProvider, PROTECT, null=True, related_name='servers')
region         = CharField(max_length=50, blank=True)
```

---

## 关键实现

### BaseDictViewSet（字典通用视图）

- 权限 `IsAdminOrReadCreate`：普通用户可查看/新增，修改与删除仅管理员（服务器接口同权限）
- 列表支持 `enabled=true|false`、`keyword`（名称/编码模糊）过滤
- `destroy` 捕获 `ProtectedError`，返回友好错误：`「生产」仍被 3 台服务器引用，无法删除`

### ServerViewSet 字典筛选

`env` / `project_group` / `channel` / `cloud_provider` 参数值为纯数字按外键 id 过滤，
否则按字典 `code` 过滤（如 `?env=prod`），URL 可读性与前端灵活性兼顾。
列表查询使用 `select_related` 预取四个外键、`prefetch_related` 预取服务关联避免 N+1。

服务维度筛选：`service_type`（id 或 code）、`service_status`（planned/deployed/offline，
可与 `service_type` 组合）、`unassigned=service_type`（未规划任何服务的主机）、
`assigned=service_type`（仅返回有服务记录的主机，服务器分配页数据源）、
`executable=true`（仅返回已规划/已部署服务的主机，offline 不算；任务执行表单数据源，
与 task_exec 执行权限校验口径一致）。
`ServerSerializer` 附带 `services` 摘要（服务类型 + 状态 + 进程名/进程数）。

### 新增主机时分配服务

创建服务器可携带可选参数 `service_type_ids`（启用中的服务类型 id 数组），
序列化器在事务内同步创建 `planned` 记录并自动生成进程名；编辑主机时该字段被忽略
（服务调整走「配置管理 → 服务器分配」）。

### 数据迁移

- `0003`：建四张字典表 + Server 加外键字段
- `0004`：内置种子数据（环境 dev/test/prod、六个云厂商、六个常用渠道），并把存量
  `env` / `department` 文本值转换为字典记录回填外键（可重复执行、可回滚）
- `0005`：删除旧的 `env` / `department` 文本列
- `0006`：新增 `ServiceType` 字典与 `ServerService` 关联表（服务类型规划）
- `0007`：`ServerService` 新增 `process_name` / `process_count` 字段，并按分组维度
  为存量记录幂等回填进程名

---

## 接口速览

| 方法 | 路径 | 说明 |
|------|------|------|
| GET/POST | `/api/assets/servers/` | 服务器列表（多维过滤）/ 创建 |
| GET/PUT/PATCH/DELETE | `/api/assets/servers/{id}/` | 详情 / 更新 / 删除 |
| GET | `/api/assets/servers/stats/` | 按状态 / 环境 / 项目组统计 |
| GET | `/api/assets/servers/export/` | 导出 CSV（应用列表筛选参数，列与导入模板一致） |
| GET | `/api/assets/servers/import-template/` | 下载导入 CSV 模板（表头 + 示例行） |
| POST | `/api/assets/servers/import/` | CSV 批量导入（multipart：file + mode=skip/update，管理员） |
| POST | `/api/assets/servers/batch/update/` | 批量编辑普通字段（status/os/region/ssh_user/ssh_port/remark，管理员） |
| POST | `/api/assets/servers/batch/delete/` | 批量删除（存在「已部署」服务的主机拒绝，管理员） |
| POST | `/api/assets/servers/register-jumpserver/` | 注册到 JumpServer（幂等，逐台结果，管理员） |
| GET/POST | `/api/assets/project-groups/` | 项目组列表 / 创建 |
| GET/POST | `/api/assets/channels/` | 渠道列表 / 创建 |
| GET/POST | `/api/assets/environments/` | 环境列表 / 创建 |
| GET/POST | `/api/assets/cloud-providers/` | 云厂商列表 / 创建 |
| GET/POST | `/api/assets/service-types/` | 服务类型列表 / 创建 |

各字典模块均支持 `{id}/` 详情、更新、删除（被引用时删除返回 400）。

---

## 主机名与 IP 约束

- **主机名全局唯一**（DB unique）：是导入更新、JumpServer 注册查重的匹配键。
- **私网 IP（`private_ip`）必填**：ansible / JumpServer 的连接地址；跨网段可重复（NAT 场景），靠主机名区分。
- **公网 IP（`public_ip`）可空**：NAT 出局主机留空（存 null，前端空串自动转换）；有值时全局唯一（部分唯一约束
  `uniq_server_public_ip`，DRF 不校验部分约束，序列化器手动查重转友好报错）。

## CSV 导入导出

- 列：`name,private_ip,public_ip,os,cpu,memory,status,project_group,channel,environment,cloud_provider,region,channel_mode,agent,ssh_port,ssh_user,remark`，
  字典外键列填字典**编码**（兼容名称），`name`/`private_ip` 必填。
- 执行通道两列（注意 `channel` 是 CMDB「渠道」字典，与执行通道无关）：
  - `channel_mode`：`auto`（默认，按路由规则）/ `direct`（强制直连）/ `agent`（指定 Agent），兼容填中文标签；
  - `agent`：Agent **名称**（`AgentNode.name` 唯一），仅 `channel_mode=agent` 生效，其余模式导入时清空钉死目标
    （与批量编辑口径一致）；`channel_mode=agent` 而 `agent` 为空、或 Agent 名称不存在均校验拦截。
  - 文件里两列都缺省时**不改动**主机现有执行通道（避免 `mode=update` 用缺列 CSV 把钉死的 Agent 重置为 auto）。
- 导入全量校验通过才写库（原子）；任一行有错返回 400，`data.errors` 携带
  `[{line, name, errors[]}]` 逐行明细。`mode=skip`（默认）主机名已存在跳过，`mode=update` **按主机名**匹配更新；
  文件内主机名/公网 IP 重复、公网 IP 已被其他主机占用均校验拦截。
- 导出应用与列表页一致的筛选参数，带 BOM，Excel 可直接打开，可回导。

## JumpServer 注册

`common/jumpserver.py` 为公共实现（asset_mgr 接口与 method_mgr `jumpserver` 执行器共用）：
**按主机名查重幂等**（JumpServer `?name=` 为精确匹配，v4.0.2 实测），address **公网优先**
（有公网 IP 用公网，NAT 出局主机回退私网），逐台返回 `{id, name, ip, ok, msg}`（ip 为实际注册地址）。行为约束：

- **重名硬拒绝**：JumpServer 资产名唯一。与 JumpServer 已有资产重名（查重/创建间并发窗口兜底）
  抛 `ValueError` 中止整批同步（接口转 400 友好报错），不做自动改名；平台侧主机名 DB 已唯一。
- 请求忽略 `http_proxy` 等环境代理（`session.trust_env = False`），JumpServer 走内网直连。
- token 用 JumpServer 私有令牌；v4 无页面入口，需在 jms_core 容器
  `python manage.py shell` 里 `User.objects.get(username=...).create_private_token()` 生成。

连接信息读系统配置：

| key | 说明 |
|-----|------|
| `jumpserver.url` | JumpServer 地址（必填） |
| `jumpserver.token` | 个人 API Token（必填，`Authorization: Token`） |
| `jumpserver.org_id` | 组织 id（可选，多组织时写入 X-JMS-ORG） |
| `jumpserver.platform_id` | 平台 id（可选，默认 1=Linux） |
| `jumpserver.node_id` | 挂载节点 id（可选） |

## 扩展建议

- **区服（游戏服）模块**：项目组 × 渠道 × 环境 × 区服 链路，区服关联部署主机后可按区服圈选执行目标
- **云账号**：对接 `method_mgr` 的 `cloud_host` 执行器时，为云厂商挂载 AK/SK 账号（加密存储、不回显）
- SSH 连通性检测：异步任务定期探测服务器
