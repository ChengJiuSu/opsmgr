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
  // 这次要做什么（拦截器按请求方法+路径填），只用于飞书确认卡上的展示——
  // 卡片确认的摩擦比翻验证器小，写清楚「要做什么、从哪来」才有人看一眼再点
  action: '',
  loading: false,      // 正在查窗口状态（决定输验证码还是密码）
  mfaEnabled: false,   // true=输验证码，false=输登录密码（后端按此校验）
  submitting: false,
  // 授权到期时间戳（毫秒）：顶栏据此显示剩余分钟。只是提示，真正的过期判定
  // 在后端缓存 TTL —— 前端时钟不准也不会放行任何东西
  expiresAt: 0,
})

/** 验证成功后登记窗口时长（秒）。 */
export function markGranted(ttlSeconds) {
  sudoState.expiresAt = Date.now() + (Number(ttlSeconds) || 0) * 1000
}

/** 剩余秒数（0 = 无授权或已过期）。 */
export function sudoRemaining() {
  if (!sudoState.expiresAt) return 0
  return Math.max(0, Math.round((sudoState.expiresAt - Date.now()) / 1000))
}

/** 登出、改密码等场景清掉本地提示。 */
export function clearSudo() {
  sudoState.expiresAt = 0
}

let pending = null      // 进行中的验证 Promise，并发请求共用
let resolvePending = null

/** 返回 Promise<boolean>：true=已验过可重放，false=用户取消。 */
export function requestSudo(action = '') {
  if (pending) return pending
  sudoState.action = action
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
