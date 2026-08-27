import request from '../utils/request'

// API 凭证（机读接口访问控制：key + IP 白名单 + 作用域 + Webhook 签名，仅管理员）
// 列表为 DRF 原生数组（不分页）；创建/轮换响应 data.api_key 仅展示一次
export const getApiCredentials = (params) => request.get('/api/auth/api-credentials/', { params })
export const getApiScopes = () => request.get('/api/auth/api-credentials/scopes/')
export const createApiCredential = (data) => request.post('/api/auth/api-credentials/', data)
export const updateApiCredential = (id, data) => request.patch(`/api/auth/api-credentials/${id}/`, data)
export const deleteApiCredential = (id) => request.delete(`/api/auth/api-credentials/${id}/`)
export const rotateApiCredentialKey = (id) => request.post(`/api/auth/api-credentials/${id}/rotate_key/`)
export const toggleApiCredential = (id) => request.post(`/api/auth/api-credentials/${id}/toggle_enabled/`)
