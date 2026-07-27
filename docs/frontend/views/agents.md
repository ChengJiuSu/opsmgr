# Agent 管理（views/system/Agents.vue）

**路由：** `/system/agents`（仅管理员，`meta.staff`）
**API：** `src/api/agents.js`（`/api/agents/`，后端模块见 `docs/backend/modules/agent.md`）

## 功能

- 列表：名称 / 状态（在线 success · 离线 danger · 已禁用 info）/ 版本 /
  最近心跳 / 运行任务数 / 负载与磁盘空闲（心跳快照 meta）；页面 30s 自动刷新。
- 新增 / 编辑：仅名称与备注；创建成功弹**一次性令牌**对话框（复制按钮，
  关闭后不再展示，与后端「仅存哈希」口径一致）。
- 重置令牌：二次确认（旧令牌立即失效、agent 断连），同样一次性展示。
- 禁用 / 启用：禁用需二次确认（提示通道主机任务将失败）。
- 删除：二次确认；后端有未终态任务单元时 400 报错（先禁用）。
- 任务单元弹窗：该 agent 最近 50 条 AgentJob（状态 / rc / 通道错误），
  task_id 链接跳转执行日志页。

## 服务器页联动（views/assets/Servers.vue）

- 表格新增「执行通道」列：直连置灰 / agent 名 warning tag。
- 表单（仅管理员可见）「执行通道」下拉：清空 = 直连；选项来自
  `getAgents()`（普通用户不加载，避免 403 弹错）。
- 批量编辑新增「执行通道」字段：勾选后可批量切换 / 清空回退直连
  （走 `servers/batch/update/` 的 `update.agent`）。
