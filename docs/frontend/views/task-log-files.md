# 日志文件（views/task/LogFiles.vue）

**路由：** `/task/log-files`（仅管理员，`meta.staff`）
**菜单：** 任务执行 → 日志文件
**API：** `src/api/tasks.js` 的 `getLogFiles` / `getLogFileContent` / `downloadLogFile`
（`/api/exec/log-files/`，后端见 `docs/backend/modules/exec.md` 的「日志文件浏览」）

## 功能

以 `TASK_LOG_DIR`（默认 `logs/tasks/`）为根的**只读**文件浏览器，覆盖执行落盘
日志与执行现场归档（主机文件 `inventory.ini`、变量文件 `vars.yml`、执行命令
`command.txt`）。目录结构：

```
<任务 id>/
├── <执行记录 id>.log                        完整执行日志
└── <执行记录 id>.d/step<序号>[-retry<n>]/   执行现场归档
    └── direct/ | agent-<id>/                执行通道
        ├── inventory.ini
        ├── vars.yml
        └── command.txt
```

- **列表**：名称（目录/文件图标，点击进入或查看）/ 说明 / 大小 / 修改时间 /
  操作。「说明」由后端标注：根层为任务名，任务目录内为「执行日志 · 成功 ·
  时间」「执行现场 · …」，记录已删则显示「记录已删除」（文件仍可翻）。
- **导航**：面包屑逐层可点，「上级目录」按钮，行双击进入目录 / 打开文件。
- **查看弹窗**：`.log` 默认「读末尾」（追加写的日志更关心尾部），其余文件
  从头读；单次 512KB，未到末尾时底部「继续加载」按 offset 续读并追加。
  内容按 `utils/ansi.js` 渲染 ANSI 彩色，暗底 `pre` 展示。
- **下载**：原文件下载（不受 512KB 限制），文件名把目录层级拼进去，避免多个
  `vars.yml` 互相覆盖。
- **复制**：查看弹窗内「复制」按钮复制**当前已加载**的内容（未读完时提示只复制了
  已加载部分），复制前用 `stripAnsi` 剥离 ANSI 转义，日志复制出来也是纯文本。
  非 https/localhost 环境 `navigator.clipboard` 不可用，退回 `execCommand('copy')`。
  `command.txt` / `inventory.ini` / `vars.yml` 体积小，一次读完即为全量。

## 与执行日志页的分工

`views/task/TaskLog.vue` 的「执行现场」按钮走 `logs/{log_id}/scene/`，定向看
**当前这次执行**的归档；本页是不依赖数据库记录的目录浏览，用于跨任务翻历史、
或记录已被清理但文件仍在的场景。两者数据同源、权限同为管理员。

执行现场弹窗的内容区同样带「复制」按钮（显示当前文件路径 + 复制），空文件占位
文案不可复制，内容被截断时提示复制的是截断后的部分。
