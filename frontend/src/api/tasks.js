import request from '../utils/request'

// 任务模块：新建/编辑仅保存定义，执行走 execute（主机按维度实时匹配）
export const getTasks = (params) => request.get('/api/exec/tasks/', { params })
export const getTask = (id) => request.get(`/api/exec/tasks/${id}/`)
export const createTask = (data) => request.post('/api/exec/tasks/', data)
export const updateTask = (id, data) => request.patch(`/api/exec/tasks/${id}/`, data)
export const deleteTask = (id) => request.delete(`/api/exec/tasks/${id}/`)
export const getMatchedServers = (id) => request.get(`/api/exec/tasks/${id}/matched_servers/`)
export const executeTask = (id, data) => request.post(`/api/exec/tasks/${id}/execute/`, data)
export const getTaskOutput = (id) => request.get(`/api/exec/tasks/${id}/output/`)
export const getTaskLogs = (id) => request.get(`/api/exec/tasks/${id}/logs/`)
export const getTaskLogDetail = (id, logId) => request.get(`/api/exec/tasks/${id}/logs/${logId}/`)

// 剧本列表（ANSIBLE_PROJECT_DIR 内可选剧本）
export const getPlaybooks = () => request.get('/api/exec/playbooks/')

// 执行变量（任务执行注入 --extra-vars；亦供 Jenkins 等外部系统拉取）
export const getVariables = (params) => request.get('/api/exec/variables/', { params })
export const createVariable = (data) => request.post('/api/exec/variables/', data)
export const updateVariable = (id, data) => request.patch(`/api/exec/variables/${id}/`, data)
export const deleteVariable = (id) => request.delete(`/api/exec/variables/${id}/`)

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
