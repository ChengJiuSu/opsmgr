import request from '../utils/request'

export const login = (data) => request.post('/api/auth/login/', data)
export const refreshToken = (data) => request.post('/api/auth/token/refresh/', data)
export const logout = (data) => request.post('/api/auth/logout/', data)
export const getMe = () => request.get('/api/auth/me/')
export const updateMe = (data) => request.patch('/api/auth/me/', data)
export const changePassword = (data) => request.post('/api/auth/change-password/', data)

// ── MFA（二次认证）──
// 登录第二步：用密码步拿到的 mfa_token + 验证码（或恢复码）换正式令牌
export const mfaVerify = (data) => request.post('/api/auth/mfa/verify/', data)
export const getMfaStatus = () => request.get('/api/auth/mfa/status/')
// 返回 secret / uri / recovery_codes，恢复码仅此一次可见
export const mfaSetup = () => request.post('/api/auth/mfa/setup/')
export const mfaActivate = (data) => request.post('/api/auth/mfa/activate/', data)
export const mfaDisable = (data) => request.post('/api/auth/mfa/disable/', data)
export const mfaRegenerateCodes = (data) =>
  request.post('/api/auth/mfa/recovery-codes/', data)

// 敏感操作二次验证（sudo）：GET 查窗口状态，POST 开窗口
export const getSudoStatus = () => request.get('/api/auth/mfa/sudo/')
export const grantSudo = (data) => request.post('/api/auth/mfa/sudo/', data)
