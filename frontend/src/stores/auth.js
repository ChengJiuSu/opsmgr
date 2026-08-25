import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import {
  login as apiLogin,
  logout as apiLogout,
  getMe,
  mfaVerify as apiMfaVerify,
} from '../api/auth'

export const useAuthStore = defineStore('auth', () => {
  const accessToken = ref(localStorage.getItem('access_token') || '')
  const refreshToken = ref(localStorage.getItem('refresh_token') || '')
  const username = ref(localStorage.getItem('username') || '')
  const user = ref(JSON.parse(localStorage.getItem('user') || 'null'))

  // 强制策略下「已登录但未绑定 MFA」：路由守卫据此把人锁在绑定页
  const mfaEnrollmentRequired = ref(localStorage.getItem('mfa_enrollment') === '1')
  // 登录第一步拿到的挑战票据：只放内存，不落 localStorage（它等价于半个凭据，
  // 且 5 分钟就过期；刷新页面重新输密码即可）
  const mfaToken = ref('')

  const isLoggedIn = computed(() => !!accessToken.value)
  const isStaff = computed(() => !!user.value?.is_staff)

  function persistTokens(access, refresh) {
    accessToken.value = access
    refreshToken.value = refresh
    localStorage.setItem('access_token', access)
    localStorage.setItem('refresh_token', refresh)
  }

  function setEnrollmentRequired(required) {
    mfaEnrollmentRequired.value = !!required
    if (required) {
      localStorage.setItem('mfa_enrollment', '1')
    } else {
      localStorage.removeItem('mfa_enrollment')
    }
  }

  async function finishLogin(res, fallbackName) {
    persistTokens(res.access, res.refresh)
    const payload = JSON.parse(atob(res.access.split('.')[1]))
    username.value = payload.username || fallbackName
    localStorage.setItem('username', username.value)
    setEnrollmentRequired(res.mfa_enrollment_required)
    await fetchMe()
  }

  /**
   * 第一步：用户名密码。
   * 已绑定 MFA 的账号这一步拿不到访问令牌，只拿到挑战票据，
   * 返回 { mfaRequired: true } 让页面切到验证码步骤。
   */
  async function login(credentials) {
    const res = await apiLogin(credentials)
    if (res.mfa_required) {
      mfaToken.value = res.mfa_token
      return { mfaRequired: true, expiresIn: res.expires_in }
    }
    await finishLogin(res, credentials.username)
    return { mfaRequired: false }
  }

  /** 第二步：验证码或恢复码 → 正式令牌。 */
  async function verifyMfa(code) {
    if (!mfaToken.value) {
      throw new Error('验证会话已失效，请重新登录')
    }
    const res = await apiMfaVerify({ mfa_token: mfaToken.value, code })
    mfaToken.value = ''
    await finishLogin(res, username.value)
    return { recoveryCodesLeft: res.recovery_codes_left }
  }

  function cancelMfa() {
    mfaToken.value = ''
  }

  async function fetchMe() {
    const res = await getMe()
    user.value = res.data
    username.value = res.data?.username || username.value
    localStorage.setItem('user', JSON.stringify(user.value))
    localStorage.setItem('username', username.value)
    return user.value
  }

  function clear() {
    accessToken.value = ''
    refreshToken.value = ''
    username.value = ''
    user.value = null
    mfaToken.value = ''
    setEnrollmentRequired(false)
    localStorage.removeItem('access_token')
    localStorage.removeItem('refresh_token')
    localStorage.removeItem('username')
    localStorage.removeItem('user')
  }

  async function logout() {
    try {
      if (refreshToken.value) {
        await apiLogout({ refresh: refreshToken.value })
      }
    } catch {
      // 即使后端登出失败也清理本地状态
    } finally {
      clear()
    }
  }

  return {
    accessToken, refreshToken, username, user,
    mfaEnrollmentRequired, mfaToken,
    isLoggedIn, isStaff,
    login, verifyMfa, cancelMfa, logout, fetchMe,
    persistTokens, setEnrollmentRequired,
  }
})
