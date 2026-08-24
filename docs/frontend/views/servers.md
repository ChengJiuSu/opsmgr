# 服务器管理（Servers.vue）

**路由：** `/assets/servers`  
**文件：** `src/views/assets/Servers.vue`

---

## 功能

- 服务器资产完整 CRUD
- 搜索名称 / 私网 IP / 公网 IP（边输边查，300ms 防抖）
- 按状态 / 环境 / 项目组 / 渠道 / 云厂商筛选（即时生效）
- 分页（20 / 50 / 100 条/页）
- 新增 / 编辑弹窗（单一组件复用）
- 删除前二次确认

---

## 组件结构

```
Servers
├── 工具栏
│   ├── 状态筛选（el-select）
│   ├── 环境筛选（el-select）
│   └── 新增按钮
├── 数据表格（el-table）
│   ├── 名称、私网 IP、公网 IP（NAT 主机留空显示 -）、操作系统
│   ├── 配置（CPU核 / 内存GB，计算列）
│   ├── 环境（Tag 着色）
│   ├── 部门
│   ├── 状态（Tag 着色）
│   └── 操作（编辑 / 删除）
├── 分页（el-pagination）
└── 弹窗（el-dialog，新增/编辑复用）
    └── el-form（双列布局）
        ├── 名称（必填、全局唯一）、私网 IP（必填）、公网 IP（可空，NAT 出局主机留空）
        ├── 操作系统、CPU、内存
        ├── 环境、状态
        ├── 部门
        └── 备注（textarea）
```

---

## 状态枚举

| 值 | Tag 类型 | 显示文字 |
|----|----------|----------|
| `running` | `success` | 运行中 |
| `stopped` | `danger` | 已停止 |
| `maintenance` | `warning` | 维护中 |

## 环境枚举

| 值 | Tag 类型 |
|----|----------|
| `prod` | `danger` |
| `test` | `warning` |
| `dev` | `info` |

---

## 核心逻辑

### 列表加载

```javascript
async function fetchData() {
  const params = { page: page.value, page_size: pageSize.value }
  if (filter.status) params.status = filter.status
  if (filter.env)    params.env    = filter.env
  const res = await getServers(params)
  tableData.value = res.data?.results || []
  total.value     = res.data?.total   || 0
}
```

### 弹窗复用（新增 / 编辑）

```javascript
function openDialog(row = null) {
  editRow.value = row
  // row 存在 → 编辑模式，填充表单；否则重置为空
  Object.assign(form, row ? { ...row } : emptyForm())
  dialogVisible.value = true
}
```

### 删除

```javascript
async function handleDelete(row) {
  await ElMessageBox.confirm(`确定删除服务器「${row.name}」吗？`, '警告', { type: 'warning' })
  await deleteServer(row.id)
  fetchData()
}
```

---

## 执行通道（三态 + 路由规则）

列「执行通道」展示的是**解析后的实际通道**，不是主机上存的字段：

| 展示 | 含义 |
|------|------|
| `Agent 名` + `主机` | 主机 `channel_mode='agent'`，钉死到该 Agent |
| `Agent 名` + `规则` | 主机 `channel_mode='auto'`，命中项目组/环境路由规则 |
| `直连` | 无规则命中 |
| `直连` + `强制` | 主机 `channel_mode='direct'`，忽略规则 |

字段来自后端 `effective_agent_name` / `channel_source`（列表序列化批量解析，
无 N+1）。该列对所有用户可见；三态表单与批量编辑仅管理员可见（Agent 选项走
仅管理员的 `/api/agents/`，普通用户不加载以免 403）。

规则在「系统管理 → Agent 管理 → 通道路由」维护，解析口径见
`docs/backend/modules/agent.md`。表单选「指定 Agent」才出现 Agent 下拉，
切走时自动清空——避免提交不生效的残留值。
