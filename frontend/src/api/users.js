import request from '../utils/request'

export const getUsers = (params) => request.get('/api/auth/users/', { params })
export const createUser = (data) => request.post('/api/auth/users/', data)
export const updateUser = (id, data) => request.patch(`/api/auth/users/${id}/`, data)
export const deleteUser = (id) => request.delete(`/api/auth/users/${id}/`)
export const toggleUserActive = (id) => request.post(`/api/auth/users/${id}/toggle_active/`)
export const resetUserPassword = (id, data) =>
  request.post(`/api/auth/users/${id}/reset_password/`, data)
export const resetUserMfa = (id) => request.post(`/api/auth/users/${id}/reset_mfa/`)

// 角色权限（模块矩阵 + 能力位，仅系统管理）；列表不分页，返回 DRF 原生数组
export const getRoles = () => request.get('/api/auth/roles/')
export const getRoleMeta = () => request.get('/api/auth/roles/meta/')
export const createRole = (data) => request.post('/api/auth/roles/', data)
export const updateRole = (id, data) => request.patch(`/api/auth/roles/${id}/`, data)
export const deleteRole = (id) => request.delete(`/api/auth/roles/${id}/`)
