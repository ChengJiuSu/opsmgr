import request from '../utils/request'

export const getServers = (params) => request.get('/api/assets/servers/', { params })
export const createServer = (data) => request.post('/api/assets/servers/', data)
export const updateServer = (id, data) => request.put(`/api/assets/servers/${id}/`, data)
export const deleteServer = (id) => request.delete(`/api/assets/servers/${id}/`)

// 服务器批量操作（仅管理员）
export const batchUpdateServers = (data) => request.post('/api/assets/servers/batch/update/', data)
export const batchDeleteServers = (data) => request.post('/api/assets/servers/batch/delete/', data)
// 注册到 JumpServer：逐台调用外部 API（经 Agent 通道时还要等 agent 回传），放宽超时
export const registerJumpServer = (data) =>
  request.post('/api/assets/servers/register-jumpserver/', data, { timeout: 300000 })

// CSV 导入导出（blob 响应由调用方触发下载）
export const exportServers = (params) =>
  request.get('/api/assets/servers/export/', { params, responseType: 'blob' })
export const getServerImportTemplate = () =>
  request.get('/api/assets/servers/import-template/', { responseType: 'blob' })
export const importServers = (formData) =>
  request.post('/api/assets/servers/import/', formData, {
    headers: { 'Content-Type': 'multipart/form-data' }, timeout: 60000,
  })

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
