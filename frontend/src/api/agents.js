import request from '../utils/request'

// Agent 管理（NAT 出口执行代理，仅管理员）
// 列表为 DRF 原生数组（不分页）；创建/重置令牌响应 data.token 仅展示一次
export const getAgents = () => request.get('/api/agents/')
export const createAgent = (data) => request.post('/api/agents/', data)
export const updateAgent = (id, data) => request.patch(`/api/agents/${id}/`, data)
export const deleteAgent = (id) => request.delete(`/api/agents/${id}/`)
export const regenAgentToken = (id) => request.post(`/api/agents/${id}/regen_token/`)
export const getAgentJobs = (id) => request.get(`/api/agents/${id}/jobs/`)

// 通道路由规则（项目组 + 可选环境 → agent）；列表不分页，返回 DRF 原生数组
export const getAgentRoutes = (params) => request.get('/api/agent-routes/', { params })
export const createAgentRoute = (data) => request.post('/api/agent-routes/', data)
export const updateAgentRoute = (id, data) => request.patch(`/api/agent-routes/${id}/`, data)
export const deleteAgentRoute = (id) => request.delete(`/api/agent-routes/${id}/`)
// 建规则前的影响面预览：该维度下 channel_mode=auto 的主机
export const previewAgentRoute = (params) => request.get('/api/agent-routes/preview/', { params })
