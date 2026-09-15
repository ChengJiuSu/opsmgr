# 配置管理模块（config_mgr）

**路由前缀：** `/api/config/`  
**App Label：** `config_mgr`

---

## 职责

对服务器进行 CMDB 维度的**批量归属分配**（项目组 / 环境 / 渠道 / 云厂商 / 地域），
以及**服务类型规划**（规划 → 部署 → 下线的三态生命周期管理）。

- 本模块**不建模型**，数据以 `asset_mgr` 为唯一数据源（与 `dashboard` 同为纯视图 app）
- 前端「配置管理」菜单 = 五个字典维护页 + 服务器分配页（字典 CRUD 接口仍在 `/api/assets/` 下）
- 服务器分配页**仅展示已规划服务的主机**（列表带 `assigned=service_type`）；
  未规划的新主机通过页内「分配服务」表单（或新增主机时的分配服务下拉）纳管。
  分配服务表单的**项目组 / 渠道 / 环境为强条件**：三个都选齐才拉取候选主机
  （复用服务器列表的 `project_group`/`channel`/`env` + `unassigned=service_type`
  组合参数），选齐前「目标主机」下拉禁用。分配是「项目组 × 渠道 × 环境」维度下的
  规划结果，少一个维度选出来的是跨维度混合清单；切换任一维度会清空已选主机，
  避免把上一组条件下勾好的主机带进新条件提交
- 服务器逐台编辑仍走资产管理的服务器接口；本模块解决"圈选一批 → 统一归属/规划"的场景
- **部署前置规划**：部署类平台方法（`method_mgr.Method.service_type`）执行前，
  task_exec 会校验目标主机已存在该服务类型的 planned/deployed 记录，未规划则拦截

---

## 文件结构

```
apps/config_mgr/
├── __init__.py
├── apps.py          # AppConfig
├── views.py         # BatchAssignView / AllocationOverviewView / Service{Plan,Mark,Unplan}View
├── urls.py          # 路由
└── tests.py         # 分配语义 / 校验 / 权限 / 概览测试
```

---

## 接口

### 批量分配

```
POST /api/config/allocation/assign/    （仅管理员）
```

**请求体：**

```json
{
  "server_ids": [1, 2, 3],
  "assign": {
    "project_group": 2,        // 提供且非 null → 设置
    "cloud_provider": null,    // 提供且为 null → 清空
    "region": "cn-hangzhou"    // 文本字段，"" 表示清空
  }                            // 未提供的字段保持不动
}
```

**语义与校验：**

| 规则 | 说明 |
|------|------|
| 部分更新 | `assign` 中未出现的字段不改动，出现且为 `null` 则清空 |
| 归属必填 | `project_group` / `channel` / `environment` 传 `null` 返回 400（只能改成另一个字典项）：主机录入时已强制选齐，清空等于绕过新增校验造出无归属主机。弹窗里这三项也不再提供「清空」选项 |
| 字典校验 | 分配的字典项必须存在且 `enabled=true`，否则 400 |
| 服务器校验 | `server_ids` 必须全部存在，否则 400 |
| 原子性 | 事务内单条 UPDATE 批量完成，并手动刷新 `updated_at` |

**成功响应（200）：** `{"code": 0, "data": {"updated": 3}}`

### 分配概览

```
GET /api/config/allocation/overview/    （登录即可）
```

**成功响应（200）：**

```json
{
  "code": 0,
  "data": {
    "total": 10,
    "unassigned": {"project_group": 3, "environment": 1, "channel": 5, "cloud_provider": 2, "service_type": 4},
    "by_project": {"仙侠项目组": 4, "未分配": 3},
    "by_env": {"生产": 6, "未分配": 1},
    "by_channel": {"官服": 2, "未分配": 5},
    "by_service": {"游戏服": {"planned": 2, "deployed": 3, "offline": 0}}
  }
}
```

### 服务类型规划（均仅管理员）

```
POST /api/config/services/plan/      批量规划：创建 planned 记录（部署前置步骤）
POST /api/config/services/mark/      状态流转：planned→deployed→offline→planned
POST /api/config/services/unplan/    取消规划：删除 planned 记录
POST /api/config/services/process/   修改进程数（只允许调大）
```

**请求体：**

```json
// plan：幂等，主机已存在该服务类型记录（任意状态）时跳过；
// process_count 可选（默认 1）；进程名自动生成（<服务类型code>-<两位序号>，
// 序号在相同云厂商+项目组+环境+渠道的主机范围内按服务类型递增），
// 响应附 process_names 映射（"<server_id>-<service_type_id>" -> 进程名）
{"server_ids": [1, 2], "service_type_ids": [3, 4], "process_count": 2}

// mark：所有主机必须已有该服务记录，且当前状态允许流转到目标状态
{"server_ids": [1, 2], "service_type_id": 3, "status": "deployed"}

// unplan：仅「已规划」可取消；已部署/已下线记录保留追溯，禁止删除
{"server_ids": [1, 2], "service_type_id": 3}

// process：修改进程数。只允许调大——目标值小于任一记录当前进程数时整批拒绝
//（缩容走下线流程）；进程名保持稳定不随进程数变化
{"server_ids": [1, 2], "service_type_id": 3, "process_count": 4}
```

**状态流转规则（`ServerService.STATUS_TRANSITIONS`）：**

| 当前状态 | 允许流转到 | 说明 |
|------|------|------|
| `planned` | `deployed` | 部署完成登记 |
| `deployed` | `offline` / `planned` | 下线（保留记录）/ 误标记回退 |
| `offline` | `planned` | 重新规划 |

---

## 配套能力

服务器列表接口（`GET /api/assets/servers/`）新增 `unassigned` 过滤参数，
供分配页快速定位漏配主机：

| 取值 | 说明 |
|------|------|
| `project_group` / `environment` / `channel` / `cloud_provider` | 指定维度未分配 |
| `any` | 任一归属维度未分配 |
| `service_type` | 未规划任何服务 |

另支持 `service_type=<id|code>`、`service_status=<planned|deployed|offline>` 组合筛选，
以及 `assigned=service_type`（仅返回有服务记录的主机，服务器分配页列表数据源）。
以上参数取值非法一律 400（不静默忽略）；**机读凭证**（X-API-KEY）使用服务/分配类
筛选时必须同时带 `project_group` + `channel` + `env`，详见
`docs/backend/modules/assets.md` 的「机读通道」。

---

## 扩展建议

- 分配历史：记录每次批量分配的操作明细（当前依赖操作审计的请求体记录）
- 按区服分配：区服模块落地后，支持"区服 → 主机"的批量绑定
- Excel 导入分配：按名称/IP 匹配批量导入归属关系
