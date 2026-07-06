# 概览仪表板（Dashboard.vue）

**路由：** `/dashboard`  
**文件：** `src/views/Dashboard.vue`

---

## 功能

- 展示系统核心统计数据（4 个统计卡片）
- 展示最新服务器状态列表（最多 10 条）
- 展示最近操作记录（最多 10 条）
- 页面加载时并行请求两个接口

---

## 组件结构

```
Dashboard
├── 统计卡片行（el-row + el-col × 4）
│   ├── 服务器总数
│   ├── 运行中数量
│   ├── 维护中数量
│   └── 今日操作数
└── 数据表格行（el-row × 2列）
    ├── 服务器状态表（名称、IP、环境、状态）
    └── 最近操作记录（用户、方法、路径、时间）
```

---

## 数据加载

```javascript
onMounted(async () => {
  // 并行请求，任一失败不影响另一个
  const [serverRes, logRes] = await Promise.allSettled([
    getServers({ page_size: 10 }),
    getAuditLogs({ page_size: 10 }),
  ])

  // 统计卡片从服务器列表计算
  statCards.value[0].value = total          // 总数
  statCards.value[1].value = running.length // 运行中
  statCards.value[2].value = maint.length   // 维护中
  statCards.value[3].value = logs.length    // 今日操作
})
```

---

## 辅助函数

```javascript
const statusType  = (s) => ({ running: 'success', stopped: 'danger', maintenance: 'warning' }[s])
const methodType  = (m) => ({ GET: 'info', POST: 'success', PUT: 'warning', DELETE: 'danger' }[m])
const formatTime  = (t) => t?.slice(5, 16).replace('T', ' ')   // 截取 MM-DD HH:mm
```
