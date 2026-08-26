/**
 * 敏感操作二次验证（sudo）的全局协调器。
 *
 * 配置面的写操作后端会返回 403 + error_code=sudo_required。响应拦截器调
 * requestSudo() 弹出验证框，用户验过后**重放原请求** —— 这样各个页面不用自己
 * 处理二次验证，加一个受控接口也不需要改前端。
 *
 * 单例状态 + Promise：同一时刻只弹一个框；并发的多个受控请求共用同一次验证。
 */
import { reactive } from 'vue'

export const sudoState = reactive({
  visible: false,
  loading: false,      // 正在查窗口状态（决定输验证码还是密码）
  mfaEnabled: false,   // true=输验证码，false=输登录密码（后端按此校验）
  submitting: false,
})

let pending = null      // 进行中的验证 Promise，并发请求共用
let resolvePending = null

/** 返回 Promise<boolean>：true=已验过可重放，false=用户取消。 */
export function requestSudo() {
  if (pending) return pending
  // 该输验证码还是密码由弹窗打开时向后端确认（GET /api/auth/mfa/sudo/）：
  // 用户可能刚在别处绑定了 MFA，本地缓存的 user 是旧的
  sudoState.visible = true
  pending = new Promise((resolve) => {
    resolvePending = resolve
  })
  return pending
}

/** 弹窗组件调用：验证成功 / 取消。 */
export function settleSudo(granted) {
  sudoState.visible = false
  sudoState.submitting = false
  const resolve = resolvePending
  pending = null
  resolvePending = null
  resolve?.(!!granted)
}
