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

底部保留原有的统计卡片、近 7 天操作趋势与最近操作记录（口径调整排在 P2/P3）。

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
├── 统计卡片行（/api/dashboard/stats/）
└── 趋势 + 最近操作记录
```

---

## 数据来源与权限

值班区数据全部来自 `/api/dashboard/overview/`，**块的有无由后端按角色矩阵裁剪**
（`apps/dashboard/services.build_overview`）：无 `cloud` 权限不会拿到证书名与内网
域名，无 `exec` 不会拿到任务名，无 `agent` 时 `agents.visible=false`。前端不做
权限判断，只按响应渲染。

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

## 空状态

`alerts` 与 `todos` 同时为空时显示一行绿字「当前无异常，也没有等待处理的事项」，
不渲染空卡片——一屏空框会让人以为接口挂了。
