import request from '../utils/request'

// 构建制品（Jenkins CI 上报，平台侧只读/删除；下载直接用后端返回的带签名 download_url）
export const getArtifacts = (params) => request.get('/api/artifacts/', { params })
export const deleteArtifact = (id) => request.delete(`/api/artifacts/${id}/`)
