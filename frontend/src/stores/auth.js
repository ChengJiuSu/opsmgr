import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { login as apiLogin, logout as apiLogout, getMe } from '../api/auth'

export const useAuthStore = defineStore('auth', () => {
  const accessToken = ref(localStorage.getItem('access_token') || '')
  const refreshToken = ref(localStorage.getItem('refresh_token') || '')
  const username = ref(localStorage.getItem('username') || '')
  const user = ref(JSON.parse(localStorage.getItem('user') || 'null'))

  const isLoggedIn = computed(() => !!accessToken.value)
  const isStaff = computed(() => !!user.value?.is_staff)

  function persistTokens(access, refresh) {
    accessToken.value = access
    refreshToken.value = refresh
    localStorage.setItem('access_token', access)
    localStorage.setItem('refresh_token', refresh)
  }

  async function login(credentials) {
    const res = await apiLogin(credentials)
    persistTokens(res.access, res.refresh)
    const payload = JSON.parse(atob(res.access.split('.')[1]))
    username.value = payload.username || credentials.username
    localStorage.setItem('username', username.value)
    await fetchMe()
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
    isLoggedIn, isStaff,
    login, logout, fetchMe,
  }
})
