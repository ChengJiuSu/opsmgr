import request from '../utils/request'

export const getAuditLogs = (params) => request.get('/api/audit/logs/', { params })
