// 任务执行日志 WebSocket 客户端（/ws/task-logs/<taskId>/）。
// 连接成功后由服务端推送快照与增量；连接失败/中断则回调 onFallback，
// 由调用方回退到 output 轮询接口（未部署 daphne / channel layer 不可用时）。

// token 走子协议（['jwt', <token>]），避免出现在 URL 与 nginx 访问日志里
const TOKEN_SUBPROTOCOL = 'jwt'
// 保活 ping：服务端回 pong，兼作连接可用性探针（nginx /ws/ 读超时 300s）
const PING_INTERVAL = 15000
// 看门狗：连 pong 都收不到（连接已死但没有 close 事件，如链路被中间设备掐掉）
// 即回退轮询。任务长时间无输出不会误触发——pong 本身就算活动
const STALE_TIMEOUT = 60000

function wsUrl(taskId) {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${window.location.host}/ws/task-logs/${taskId}/`
}

/**
 * 打开日志流。
 * @param {number} taskId
 * @param {object} handlers onFull(frame)/onDelta(frame)/onSteps(steps)/onStatus(data)/onFallback(reason)
 *   onFull/onDelta 收到的是整帧（含 head/output/start/offset 绝对偏移信息，
 *   由 utils/taskLogBuffer 拼接），不是裸文本。
 * @returns {{close: Function}} 调用 close() 主动断开（不会触发 onFallback）
 */
export function openTaskLogStream(taskId, handlers = {}) {
  const { onFull, onDelta, onSteps, onStatus, onFallback } = handlers
  const token = localStorage.getItem('access_token') || ''
  let socket = null
  let pingTimer = null
  let staleTimer = null
  let closedByUs = false
  let opened = false

  function cleanup() {
    if (pingTimer) { clearInterval(pingTimer); pingTimer = null }
    if (staleTimer) { clearTimeout(staleTimer); staleTimer = null }
    if (socket) {
      socket.onopen = socket.onmessage = socket.onerror = socket.onclose = null
      socket = null
    }
  }

  function fallback(reason) {
    if (closedByUs) return
    cleanup()
    onFallback?.(reason)
  }

  try {
    socket = new WebSocket(wsUrl(taskId), [TOKEN_SUBPROTOCOL, token])
  } catch {
    onFallback?.('websocket-unavailable')
    return { close() {} }
  }

  function armWatchdog() {
    if (staleTimer) clearTimeout(staleTimer)
    staleTimer = setTimeout(() => fallback('stale'), STALE_TIMEOUT)
  }

  socket.onopen = () => {
    opened = true
    armWatchdog()
    pingTimer = setInterval(() => {
      if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'ping' }))
    }, PING_INTERVAL)
  }

  socket.onmessage = (event) => {
    let msg
    try {
      msg = JSON.parse(event.data)
    } catch {
      return
    }
    armWatchdog()  // 任何下行帧（含 pong）都算连接活着
    if (msg.kind === 'full') {
      onFull?.(msg)
      // 快照同时带模块表与状态，首帧即可画出模块列表、切出暂停操作条
      // （空数组不算：[] 是真值，照发会把已有模块表抹掉）
      if (msg.steps?.length) onSteps?.(msg.steps)
      if (msg.status) onStatus?.(msg)
    } else if (msg.kind === 'delta') {
      onDelta?.(msg)
    } else if (msg.kind === 'steps') {
      if (msg.steps?.length) onSteps?.(msg.steps)
    } else if (msg.kind === 'status') {
      onStatus?.(msg)
    }
  }

  // 鉴权失败（4401）/任务不存在（4404）/服务端未就绪：统一回退轮询
  socket.onerror = () => fallback(opened ? 'error' : 'connect-failed')
  socket.onclose = (event) => fallback(`closed:${event.code}`)

  return {
    close() {
      closedByUs = true
      const sock = socket
      cleanup()
      if (sock && (sock.readyState === WebSocket.OPEN || sock.readyState === WebSocket.CONNECTING)) {
        sock.close()
      }
    },
  }
}
