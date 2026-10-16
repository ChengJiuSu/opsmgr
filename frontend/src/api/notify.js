import request from '../utils/request'

// ── 消息通知（system 模块 write 档，写操作 sudo）──
// 可订阅的事件清单由后端代码注册（events.register），页面不写死
export const getEventTypes = () => request.get('/api/notify/events/')
// 规则表单的会话下拉：只要 notify 档，不必为选会话去开 system（飞书会话白名单管理）
export const getChatOptions = () => request.get('/api/notify/chats/')

// 群机器人：token / 签名密钥只写不回显，留空表示不修改
export const getRobots = (params) => request.get('/api/notify/robots/', { params })
export const createRobot = (data) => request.post('/api/notify/robots/', data)
export const updateRobot = (id, data) => request.patch(`/api/notify/robots/${id}/`, data)
export const deleteRobot = (id) => request.delete(`/api/notify/robots/${id}/`)
export const testRobot = (id) => request.post(`/api/notify/robots/${id}/test/`)

// 订阅规则
export const getRules = (params) => request.get('/api/notify/rules/', { params })
export const createRule = (data) => request.post('/api/notify/rules/', data)
export const updateRule = (id, data) => request.put(`/api/notify/rules/${id}/`, data)
export const patchRule = (id, data) => request.patch(`/api/notify/rules/${id}/`, data)
export const deleteRule = (id) => request.delete(`/api/notify/rules/${id}/`)
export const testRule = (id) => request.post(`/api/notify/rules/${id}/test/`)

// 发送记录（只读）
export const getDeliveries = (params) => request.get('/api/notify/deliveries/', { params })
