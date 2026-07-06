import request from '../utils/request'

// 执行任务
export const getTasks = (params) => request.get('/api/exec/tasks/', { params })
export const getTask = (id) => request.get(`/api/exec/tasks/${id}/`)
export const createTask = (data) => request.post('/api/exec/tasks/', data)
export const deleteTask = (id) => request.delete(`/api/exec/tasks/${id}/`)
export const rerunTask = (id) => request.post(`/api/exec/tasks/${id}/rerun/`)
export const getTaskOutput = (id) => request.get(`/api/exec/tasks/${id}/output/`)

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
