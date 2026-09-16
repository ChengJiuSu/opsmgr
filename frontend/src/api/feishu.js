import request from '../utils/request'

// ── 飞书登录（SSO）──
// status 是公开接口：登录页在未登录时据此决定要不要渲染「飞书登录」按钮
export const getSsoStatus = () => request.get('/api/feishu/sso/status/')
export const getAuthorizeUrl = () => request.get('/api/feishu/sso/authorize/')
export const ssoLogin = (data) => request.post('/api/feishu/sso/callback/', data)
// 当前用户的绑定状态（个人页展示，只读；解绑属于管理动作）
export const getMyBinding = () => request.get('/api/feishu/sso/binding/')

// ── 应用配置（system 模块 write 档 + sudo）──
// 三把密钥只写不回显，留空表示不修改；missing_fields 告诉页面还缺什么
export const getApp = () => request.get('/api/feishu/app/')
export const updateApp = (data) => request.put('/api/feishu/app/', data)
// 轮换回调标识 = 换回调地址，飞书后台必须同步改，故是独立动作
export const rotateCallbackSlug = () => request.post('/api/feishu/app/rotate-slug/')

// ── 会话白名单 ──
export const getChannels = (params) => request.get('/api/feishu/channels/', { params })
export const createChannel = (data) => request.post('/api/feishu/channels/', data)
export const updateChannel = (id, data) => request.put(`/api/feishu/channels/${id}/`, data)
export const deleteChannel = (id) => request.delete(`/api/feishu/channels/${id}/`)

// ── 绑定关系（account 模块 write 档 + sudo）──
// 没有更新接口：改绑 = 解绑再绑，两步各自留痕
export const getBindings = (params) => request.get('/api/feishu/bindings/', { params })
export const createBinding = (data) => request.post('/api/feishu/bindings/', data)
export const deleteBinding = (id) => request.delete(`/api/feishu/bindings/${id}/`)
// 按邮箱/手机号在飞书通讯录里反查 union_id / open_id（手工绑定用）
export const lookupFeishuUser = (data) => request.post('/api/feishu/bindings/lookup/', data)
