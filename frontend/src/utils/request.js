import axios from 'axios'
import { ElMessage } from 'element-plus'
import router from '../router'

const baseURL = import.meta.env.VITE_API_BASE_URL || ''

const request = axios.create({
  baseURL,
  timeout: 10000,
})

// 刷新 token 专用实例：不走业务拦截器，避免递归
const rawAxios = axios.create({ baseURL, timeout: 10000 })

// 不做静默刷新的接口（自身 401 即视为登录态失效）
const NO_REFRESH_PATHS = ['/api/auth/login/', '/api/auth/token/refresh/', '/api/auth/logout/']

let refreshing = null   // 进行中的刷新请求，并发 401 共用同一个 Promise

function clearAuth() {
  localStorage.removeItem('access_token')
  localStorage.removeItem('refresh_token')
  localStorage.removeItem('username')
  localStorage.removeItem('user')
}

// 刷新成功后同步 pinia store（动态导入避免循环依赖）
async function syncStore(access, refresh) {
  try {
    const { useAuthStore } = await import('../stores/auth')
    const store = useAuthStore()
    store.accessToken = access
    store.refreshToken = refresh
  } catch {
    // store 未初始化时忽略，localStorage 是权威来源
  }
}

// 用 refresh token 换新 access token
function doRefresh() {
  if (refreshing) return refreshing
  const refresh = localStorage.getItem('refresh_token')
  if (!refresh) return Promise.reject(new Error('no refresh token'))

  refreshing = rawAxios
    .post('/api/auth/token/refresh/', { refresh })
    .then(async (res) => {
      const access = res.data.access
      const newRefresh = res.data.refresh || refresh   // ROTATE_REFRESH_TOKENS 开启时会返回新 refresh
      localStorage.setItem('access_token', access)
      localStorage.setItem('refresh_token', newRefresh)
      await syncStore(access, newRefresh)
      return access
    })
    .finally(() => {
      refreshing = null
    })
  return refreshing
}

// 请求拦截：自动附加 JWT token
request.interceptors.request.use(
  (config) => {
    const token = localStorage.getItem('access_token')
    if (token) {
      config.headers.Authorization = `Bearer ${token}`
    }
    return config
  },
  (error) => Promise.reject(error),
)

// 响应拦截：401 静默刷新并重放原请求，其余统一提示
request.interceptors.response.use(
  (response) => response.data,
  async (error) => {
    const status = error.response?.status
    const config = error.config || {}

    if (status === 401) {
      const skip = !config.url || NO_REFRESH_PATHS.some((p) => config.url.includes(p))
      if (!skip && !config._retried && localStorage.getItem('refresh_token')) {
        config._retried = true
        try {
          const access = await doRefresh()
          config.headers = { ...config.headers, Authorization: `Bearer ${access}` }
          return await request(config)
        } catch {
          // 刷新失败，走下面的登出流程
        }
      }
      clearAuth()
      router.push('/login')
      ElMessage.error('登录已过期，请重新登录')
    } else if (status === 403) {
      // 强制 MFA 但未绑定：后端认证层返回该错误码，直接把人送去绑定页
      if (error.response?.data?.error_code === 'mfa_enrollment_required') {
        localStorage.setItem('mfa_enrollment', '1')
        if (router.currentRoute.value.name !== 'SecurityMfa') {
          ElMessage.warning('按安全策略需先绑定二次认证（MFA）')
          router.push({ name: 'SecurityMfa' })
        }
      } else {
        ElMessage.error('权限不足')
      }
    } else {
      const msg = error.response?.data?.msg || error.message || '请求失败'
      ElMessage.error(msg)
    }
    return Promise.reject(error)
  },
)

export default request
