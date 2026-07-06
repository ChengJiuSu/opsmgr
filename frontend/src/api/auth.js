import request from '../utils/request'

export const login = (data) => request.post('/api/auth/login/', data)
export const refreshToken = (data) => request.post('/api/auth/token/refresh/', data)
export const logout = (data) => request.post('/api/auth/logout/', data)
export const getMe = () => request.get('/api/auth/me/')
export const updateMe = (data) => request.patch('/api/auth/me/', data)
export const changePassword = (data) => request.post('/api/auth/change-password/', data)
