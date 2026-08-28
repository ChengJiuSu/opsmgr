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

  // ── 角色权限快照（后端 roles.permission_snapshot）──
  // 只用来决定按钮/菜单可见性：真正的判定始终在后端，快照过期最多让人多看到一个
  // 按钮，点下去照样被拒。
  const perms = computed(() => user.value?.permissions || null)
  const LEVELS = ['none', 'read', 'read_create', 'write']
  const METHOD_LEVEL = { read: 'read', create: 'read_create', write: 'write' }

  /**
   * 是否具备某模块的某档权限。
   * @param {string} module 模块标识（见后端 roles.MODULES：cmdb / dict / exec / method /
   *   variable / artifact / credential / cloud / cloud_account / agent / account / system / audit）
   * @param {'read'|'create'|'write'} action 默认 read
   */
  function can(module, action = 'read') {
    // 快照缺失（老 token / 未取到 me）时回退 is_staff，避免整站按钮全灰
    const modules = perms.value?.modules
    if (!modules) return isStaff.value
    // 快照里**根本没有这个键** = 这份快照比该模块还老（后端拆分模块后，登录时
    // 缓存的旧快照只有旧模块名）。此时按 none 处理会把管理员的菜单整块藏掉，
    // 且刷新页面也好不了——快照只在登录时取。判为「不知道」，回退 is_staff，
    // 由 Layout 挂载时的 fetchMe 把快照换新。可见性错了最多多显示一个按钮，
    // 点下去仍由后端判定
    if (!(module in modules)) return isStaff.value
    const level = modules[module] || 'none'
    return LEVELS.indexOf(level) >= LEVELS.indexOf(METHOD_LEVEL[action] || 'read')
  }

  /** 独立能力位：can_execute / can_exec_prod。 */
  function capability(name) {
    if (!perms.value) return isStaff.value
    return !!perms.value[name]
  }

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
    isLoggedIn, isStaff, perms, can, capability,
    login, verifyMfa, cancelMfa, logout, fetchMe,
    persistTokens, setEnrollmentRequired,
  }
})
