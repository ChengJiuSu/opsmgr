import request from '../utils/request'

export const getServers = (params) => request.get('/api/assets/servers/', { params })
export const createServer = (data) => request.post('/api/assets/servers/', data)
export const updateServer = (id, data) => request.put(`/api/assets/servers/${id}/`, data)
export const deleteServer = (id) => request.delete(`/api/assets/servers/${id}/`)

// CMDB 字典模块通用接口工厂：项目组 / 渠道 / 环境 / 云厂商
const createDictApi = (path) => ({
  list: (params) => request.get(`/api/assets/${path}/`, { params }),
  create: (data) => request.post(`/api/assets/${path}/`, data),
  update: (id, data) => request.put(`/api/assets/${path}/${id}/`, data),
  remove: (id) => request.delete(`/api/assets/${path}/${id}/`),
})

export const projectGroupApi = createDictApi('project-groups')
export const channelApi = createDictApi('channels')
export const environmentApi = createDictApi('environments')
export const cloudProviderApi = createDictApi('cloud-providers')
export const serviceTypeApi = createDictApi('service-types')

// 拉取启用中的字典项（下拉选项用，page_size 取后端上限）
export const fetchDictOptions = (api) => api.list({ enabled: 'true', page_size: 200 })
