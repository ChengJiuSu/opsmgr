import request from '../utils/request'

export const getConfigs = (params) => request.get('/api/system/configs/', { params })
export const createConfig = (data) => request.post('/api/system/configs/', data)
export const updateConfig = (id, data) => request.put(`/api/system/configs/${id}/`, data)
export const deleteConfig = (id) => request.delete(`/api/system/configs/${id}/`)
