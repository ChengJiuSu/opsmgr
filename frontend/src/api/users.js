import request from '../utils/request'

export const getUsers = (params) => request.get('/api/auth/users/', { params })
export const createUser = (data) => request.post('/api/auth/users/', data)
export const updateUser = (id, data) => request.patch(`/api/auth/users/${id}/`, data)
export const deleteUser = (id) => request.delete(`/api/auth/users/${id}/`)
export const toggleUserActive = (id) => request.post(`/api/auth/users/${id}/toggle_active/`)
export const resetUserPassword = (id, data) =>
  request.post(`/api/auth/users/${id}/reset_password/`, data)
