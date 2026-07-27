import request from '../utils/request'

// Agent 管理（NAT 出口执行代理，仅管理员）
// 列表为 DRF 原生数组（不分页）；创建/重置令牌响应 data.token 仅展示一次
export const getAgents = () => request.get('/api/agents/')
export const createAgent = (data) => request.post('/api/agents/', data)
export const updateAgent = (id, data) => request.patch(`/api/agents/${id}/`, data)
export const deleteAgent = (id) => request.delete(`/api/agents/${id}/`)
export const regenAgentToken = (id) => request.post(`/api/agents/${id}/regen_token/`)
export const getAgentJobs = (id) => request.get(`/api/agents/${id}/jobs/`)
