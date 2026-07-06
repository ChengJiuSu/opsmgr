import request from '../utils/request'

// 配置管理：服务器归属批量分配与概览
export const batchAssign = (data) => request.post('/api/config/allocation/assign/', data)
export const getAllocationOverview = () => request.get('/api/config/allocation/overview/')

// 服务类型规划：批量规划 / 状态流转 / 取消规划
export const planServices = (data) => request.post('/api/config/services/plan/', data)
export const markServices = (data) => request.post('/api/config/services/mark/', data)
export const unplanServices = (data) => request.post('/api/config/services/unplan/', data)
