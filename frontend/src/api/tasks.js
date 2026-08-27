import request from '../utils/request'

// 任务模块：新建/编辑仅保存定义，执行走 execute（主机按维度实时匹配）
export const getTasks = (params) => request.get('/api/exec/tasks/', { params })
export const getTask = (id) => request.get(`/api/exec/tasks/${id}/`)
export const createTask = (data) => request.post('/api/exec/tasks/', data)
export const updateTask = (id, data) => request.patch(`/api/exec/tasks/${id}/`, data)
export const deleteTask = (id) => request.delete(`/api/exec/tasks/${id}/`)
export const getMatchedServers = (id) => request.get(`/api/exec/tasks/${id}/matched_servers/`)
export const executeTask = (id, data) => request.post(`/api/exec/tasks/${id}/execute/`, data)
// 执行中日志轮询：带 offset 时后端只回传新增部分（delta=true 则前端追加）
export const getTaskOutput = (id, offset = 0) =>
  request.get(`/api/exec/tasks/${id}/output/`, { params: offset ? { offset } : {} })
// 暂停确认：任务流停在「暂停确认」模块时继续（continue）或中止（abort）
export const confirmTask = (id, action) => request.post(`/api/exec/tasks/${id}/confirm/`, { action })
export const getTaskLogs = (id) => request.get(`/api/exec/tasks/${id}/logs/`)
export const getTaskLogDetail = (id, logId) => request.get(`/api/exec/tasks/${id}/logs/${logId}/`)
// 执行现场归档（inventory/变量/命令，仅管理员）：不带 file 取清单，带 file 取内容
export const getTaskLogScene = (id, logId, file) =>
  request.get(`/api/exec/tasks/${id}/logs/${logId}/scene/`, { params: file ? { file } : {} })

// 日志文件浏览（仅管理员）：TASK_LOG_DIR 目录树 / 文本内容 / 原文件下载
export const getLogFiles = (path = '') => request.get('/api/exec/log-files/', { params: { path } })
export const getLogFileContent = (params) => request.get('/api/exec/log-files/content/', { params })
export const downloadLogFile = (path) =>
  request.get('/api/exec/log-files/download/', { params: { path }, responseType: 'blob' })

// 剧本列表（ANSIBLE_PROJECT_DIR 内可选剧本）
export const getPlaybooks = () => request.get('/api/exec/playbooks/')

// 执行变量（按维度加载注入 --extra-vars；亦供 Jenkins 等外部系统拉取）
export const getVariables = (params) => request.get('/api/exec/variables/', { params })
export const createVariable = (data) => request.post('/api/exec/variables/', data)
export const updateVariable = (id, data) => request.patch(`/api/exec/variables/${id}/`, data)
export const deleteVariable = (id) => request.delete(`/api/exec/variables/${id}/`)
export const getVariableVersions = (id) => request.get(`/api/exec/variables/${id}/versions/`)
export const rollbackVariable = (id, version) => request.post(`/api/exec/variables/${id}/rollback/`, { version })
// 敏感变量查看真值（管理员 + sudo 二次验证，走 POST 以进审计写操作记录）
export const revealVariable = (id) => request.post(`/api/exec/variables/${id}/reveal/`)

// 凭据
export const getCredentials = (params) => request.get('/api/exec/credentials/', { params })
export const createCredential = (data) => request.post('/api/exec/credentials/', data)
export const updateCredential = (id, data) => request.patch(`/api/exec/credentials/${id}/`, data)
export const deleteCredential = (id) => request.delete(`/api/exec/credentials/${id}/`)

// 平台方法
export const getMethods = (params) => request.get('/api/exec/methods/', { params })
export const createMethod = (data) => request.post('/api/exec/methods/', data)
export const updateMethod = (id, data) => request.patch(`/api/exec/methods/${id}/`, data)
export const deleteMethod = (id) => request.delete(`/api/exec/methods/${id}/`)
