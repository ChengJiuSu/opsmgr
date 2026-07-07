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
可与 `service_type` 组合）、`unassigned=service_type`（未规划任何服务的主机）。
`ServerSerializer` 附带 `services` 摘要（服务类型 + 状态列表）。

### 数据迁移

- `0003`：建四张字典表 + Server 加外键字段
- `0004`：内置种子数据（环境 dev/test/prod、六个云厂商、六个常用渠道），并把存量
  `env` / `department` 文本值转换为字典记录回填外键（可重复执行、可回滚）
- `0005`：删除旧的 `env` / `department` 文本列
- `0006`：新增 `ServiceType` 字典与 `ServerService` 关联表（服务类型规划）

---

## 接口速览

| 方法 | 路径 | 说明 |
|------|------|------|
| GET/POST | `/api/assets/servers/` | 服务器列表（多维过滤）/ 创建 |
| GET/PUT/PATCH/DELETE | `/api/assets/servers/{id}/` | 详情 / 更新 / 删除 |
| GET | `/api/assets/servers/stats/` | 按状态 / 环境 / 项目组统计 |
| GET/POST | `/api/assets/project-groups/` | 项目组列表 / 创建 |
| GET/POST | `/api/assets/channels/` | 渠道列表 / 创建 |
| GET/POST | `/api/assets/environments/` | 环境列表 / 创建 |
| GET/POST | `/api/assets/cloud-providers/` | 云厂商列表 / 创建 |
| GET/POST | `/api/assets/service-types/` | 服务类型列表 / 创建 |

各字典模块均支持 `{id}/` 详情、更新、删除（被引用时删除返回 400）。

---

## 扩展建议

- **区服（游戏服）模块**：项目组 × 渠道 × 环境 × 区服 链路，区服关联部署主机后可按区服圈选执行目标
- **云账号**：对接 `method_mgr` 的 `cloud_host` 执行器时，为云厂商挂载 AK/SK 账号（加密存储、不回显）
- 批量导入：支持 Excel/CSV 批量录入服务器
- SSH 连通性检测：异步任务定期探测服务器
