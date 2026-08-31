# 概览仪表板（Dashboard.vue）

**路由：** `/dashboard`
**文件：** `src/views/Dashboard.vue`
**子组件：** `src/components/dashboard/{DutyBlocks,RunningTasks,AgentStatus}.vue`

---

## 定位

这一页不是统计看板，是**值班首页**：从上到下回答三个问题——

1. **出事没？** → 异常块（无异常时整块不渲染）
2. **有没有等我处理的？** → 待处理块（暂停任务、DNS 待审批、自己没开 MFA）
3. **现在在跑什么？** → 执行中的任务 + Agent 节点心跳

再往下是「盘子有多大」：主机分布与服务规划三态、各环境当前制品版本；最后是近
7 天执行趋势与最近操作记录。

趋势图画的是**执行成败**而不是审计条数：审计中间件只记写操作，但它把「改了一个
字典项」和「往生产推了一次包」算作同样的一格，看不出这一周跑得顺不顺。没有 `exec`
权限的人回退到原来的写操作条数图。

原先那排统计卡（服务器总数 / 运行中 / 维护中 / 今日操作）已删除：`Server.status`
是人工维护字段，没人在主机停机时回来改，长期恒为 running，摆在首页只是个假数字。
今日操作数移到趋势卡的标题右侧。

---

## 组件结构

```
Dashboard
├── 值班状态卡片（/api/dashboard/overview/）
│   ├── DutyBlocks（异常：证书过期/卡死、域名与解析异常、失败执行、Agent 离线）
│   └── DutyBlocks（待处理：暂停任务、DNS 变更单、MFA 未启用）
├── 运行态行
│   ├── RunningTasks（执行中的任务：模块进度、发起人、已跑时长）
│   └── AgentStatus（在线/离线 + 心跳快照 load1 / disk_free_pct / running_jobs）
├── 资产盘子行
│   ├── AssetDistribution（主机总数 / 已部署 / 已规划待部署 / 已下线 + 按环境与
│   │   项目组的分布条，超过 6 档尾部并成「其他」）
│   └── LatestArtifacts（各「项目组 × 渠道 × 环境」当前最新版本）
└── 活动行
    ├── ExecTrend（近 7 天执行堆叠柱：成功 / 失败 / 未结束；无 exec 权限时回退
    │   审计写操作条数图，数据来自 /api/dashboard/stats/）
    └── 最近操作记录
```

---

## 数据来源与权限

值班区数据全部来自 `/api/dashboard/overview/`，**块的有无由后端按角色矩阵裁剪**
（`apps/dashboard/services.build_overview`）：无 `cloud` 权限不会拿到证书名与内网
域名，无 `exec` 不会拿到任务名，无 `agent` / `cmdb` / `artifact` 时对应块的
`visible=false`（前端整块不渲染）。前端不做权限判断，只按响应渲染。

块与明细都可能带 `link`（`{path, query}`），点击即 `router.push`：

| 块 | 落地页 |
|----|--------|
| 证书即将过期 | `/cloud/certificates?expiring_days=30` |
| 证书申请失败 / 签发卡住 | `/cloud/certificates?status=failed` / `?status=issuing` |
| 域名与解析异常 | `/cloud/domains?keyword=<域名>` |
| 失败执行（明细） | `/task/executions/:taskId/logs/:logId` |
| 暂停任务 | 该次执行的日志页，取不到日志时回退 `/task/executions?status=paused` |
| DNS 变更单 | `/cloud/domains?requests=pending`（自动打开审批抽屉） |
| Agent 离线 | `/system/agents` |
| 资产分布 | `/assets/servers`、`/config/allocation` |
| 各环境当前版本 | `/task/artifacts` |
| 执行趋势 | `/task/executions` |

对应的三个落地页在 `onMounted` 里读 `route.query` 预置筛选。

---

## 刷新策略

```javascript
timer = setInterval(tick, 30000)               // 30s 轮询
function tick() { if (!document.hidden) fetchOverview() }
document.addEventListener('visibilitychange', onVisible)  // 回到前台立即补一次
```

标签页在后台时跳过请求：值班页常年开着，不跳过等于一天白刷几千次。
`onUnmounted` 里清 timer 与事件监听。

---

## 区块显隐

页头右上角「自定义」可关掉整块（值班状态 / 运行态 / 资产盘子 / 趋势与操作记录），
选择存 `localStorage`，键按用户名分（`dashboard_sections_<username>`）——同一台机器
换个人登录不该继承上一个人的布局。读取时过滤掉未登记的 key，免得版本迭代删掉某个
区块后旧记录影响判断。

这是纯展示偏好，不占后端字段：配到库里就要连带做接口、权限与迁移，收益不成比例。

---

## 卡片对齐

同一行的两张卡内容多少不同（如「执行中的任务」空着而「Agent 节点」有五行），
默认会下沿错开。用 `:deep()` 把 `el-col` 设成 flex、卡片撑满宽高、`el-card__body`
`flex: 1`，让同行卡片等高。

---

## 空状态

`alerts` 与 `todos` 同时为空时显示一行绿字「当前无异常，也没有等待处理的事项」，
不渲染空卡片——一屏空框会让人以为接口挂了。
