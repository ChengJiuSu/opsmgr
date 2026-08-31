import request from '../utils/request'

export const getDashboardStats = () => request.get('/api/dashboard/stats/')

// 概览页值班视角：异常 / 待处理 / 运行态（块级权限由后端裁剪）
export const getDashboardOverview = () => request.get('/api/dashboard/overview/')
