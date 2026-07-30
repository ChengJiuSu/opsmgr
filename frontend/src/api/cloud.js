import request from '../utils/request'

// 云账号（仅管理员）
export const getCloudAccounts = (params) => request.get('/api/cloud/accounts/', { params })
export const createCloudAccount = (data) => request.post('/api/cloud/accounts/', data)
export const updateCloudAccount = (id, data) => request.put(`/api/cloud/accounts/${id}/`, data)
export const deleteCloudAccount = (id) => request.delete(`/api/cloud/accounts/${id}/`)
// 连通性测试：调厂商域名列表接口，放宽超时
export const testCloudAccount = (id) =>
  request.post(`/api/cloud/accounts/${id}/test_connection/`, {}, { timeout: 60000 })

// 主域名
export const getDomains = (params) => request.get('/api/cloud/domains/', { params })
export const createDomain = (data) => request.post('/api/cloud/domains/', data)
export const updateDomain = (id, data) => request.put(`/api/cloud/domains/${id}/`, data)
export const deleteDomain = (id) => request.delete(`/api/cloud/domains/${id}/`)
// 从云账号导入托管域名 / 从厂商侧同步解析记录（外部 API，放宽超时）
export const importDomains = (data) =>
  request.post('/api/cloud/domains/import_remote/', data, { timeout: 60000 })
export const syncDomainRecords = (id) =>
  request.post(`/api/cloud/domains/${id}/sync_records/`, {}, { timeout: 120000 })

// 解析记录
export const getDnsRecords = (params) => request.get('/api/cloud/dns-records/', { params })
export const createDnsRecord = (data) => request.post('/api/cloud/dns-records/', data)
export const updateDnsRecord = (id, data) => request.put(`/api/cloud/dns-records/${id}/`, data)
export const deleteDnsRecord = (id) => request.delete(`/api/cloud/dns-records/${id}/`)
export const retrySyncDnsRecord = (id) => request.post(`/api/cloud/dns-records/${id}/retry_sync/`)

// 证书
export const getCertificates = (params) => request.get('/api/cloud/certificates/', { params })
export const createCertificate = (data) => request.post('/api/cloud/certificates/', data)
export const updateCertificate = (id, data) => request.patch(`/api/cloud/certificates/${id}/`, data)
export const deleteCertificate = (id) => request.delete(`/api/cloud/certificates/${id}/`)
// 申请/续签为后台异步任务，提交即返回
export const issueCertificate = (id) => request.post(`/api/cloud/certificates/${id}/issue/`)
export const renewCertificate = (id) => request.post(`/api/cloud/certificates/${id}/renew/`)
export const downloadCertificate = (id) => request.get(`/api/cloud/certificates/${id}/download/`)
