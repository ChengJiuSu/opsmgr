import request from '../utils/request'

// 拓扑画布（元信息 CRUD）
export const getCanvases = (params) => request.get('/api/topo/canvases/', { params })
export const createCanvas = (data) => request.post('/api/topo/canvases/', data)
export const updateCanvas = (id, data) => request.patch(`/api/topo/canvases/${id}/`, data)
export const deleteCanvas = (id) => request.delete(`/api/topo/canvases/${id}/`)

// 整图读写：节点与连线一次拉一次存（后端整事务覆盖，无半保存状态）
export const getGraph = (id) => request.get(`/api/topo/canvases/${id}/graph/`)
export const saveGraph = (id, data) =>
  request.put(`/api/topo/canvases/${id}/graph/`, data, { timeout: 30000 })

// 自动编排：按画布维度拉平台已有资源生成节点与关系（幂等）
export const autofillCanvas = (id) =>
  request.post(`/api/topo/canvases/${id}/autofill/`, {}, { timeout: 60000 })

// 可引用的平台资源候选（kind = server / domain / dns_record / certificate）
export const getCandidates = (id, params) =>
  request.get(`/api/topo/canvases/${id}/candidates/`, { params })

// 节点类型与关系登记表：左侧调色板与关系下拉由后端驱动，前端不写死一份
export const getTopoMeta = () => request.get('/api/topo/meta/')

// 按新维度克隆画布（维度不符的主机会被跳过，报告里逐条列出）
export const cloneCanvas = (id, data) =>
  request.post(`/api/topo/canvases/${id}/clone/`, data, { timeout: 30000 })

// 导出 / 导入：引用型节点用自然键（主机名、域名…）而非主键，可跨实例搬运
export const exportCanvas = (id) => request.get(`/api/topo/canvases/${id}/export/`)
export const importCanvas = (data) =>
  request.post('/api/topo/import/', data, { timeout: 30000 })

// 影响面反查：这台主机 / 这个域名在哪些画布上、上下游是谁
export const getTopoImpact = (params) => request.get('/api/topo/impact/', { params })
