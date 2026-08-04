// ansible 彩色输出（ANSI SGR 转义）渲染：转成带 class 的 <span>。
// 只识别 SGR（\x1b[…m）里的复位/加粗/前景/背景色，其余转义序列（光标移动、
// 擦除行等）直接丢弃；文本先做 HTML 转义，保证输出可安全用于 v-html。

const ESC_RE = /\x1b\[([0-9;]*)([a-zA-Z])|\x1b[()][A-Za-z0-9]|\x1b[=>]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?/g

// 30-37/90-97 前景，40-47/100-107 背景 → CSS class（样式见 StepOutput.vue）
const FG = {
  30: 'black', 31: 'red', 32: 'green', 33: 'yellow',
  34: 'blue', 35: 'magenta', 36: 'cyan', 37: 'white',
  90: 'bright-black', 91: 'bright-red', 92: 'bright-green', 93: 'bright-yellow',
  94: 'bright-blue', 95: 'bright-magenta', 96: 'bright-cyan', 97: 'bright-white',
}

export function escapeHtml(text) {
  return String(text ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
}

function classOf(state) {
  const cls = []
  if (state.fg) cls.push(`ansi-fg-${state.fg}`)
  if (state.bg) cls.push(`ansi-bg-${state.bg}`)
  if (state.bold) cls.push('ansi-bold')
  return cls.join(' ')
}

function applySgr(state, params) {
  // 空参数（\x1b[m）等价于 0（复位）
  const codes = (params || '0').split(';').map((n) => Number(n || 0))
  for (let i = 0; i < codes.length; i += 1) {
    const code = codes[i]
    if (code === 0) {
      state.fg = state.bg = ''
      state.bold = false
    } else if (code === 1) {
      state.bold = true
    } else if (code === 22) {
      state.bold = false
    } else if (code === 39) {
      state.fg = ''
    } else if (code === 49) {
      state.bg = ''
    } else if (FG[code]) {
      state.fg = FG[code]
    } else if (FG[code - 10]) {
      state.bg = FG[code - 10]
    } else if (code === 38 || code === 48) {
      // 256 色/真彩色：跳过参数，统一按默认色显示（ansible 默认调色板不用）
      i += codes[i + 1] === 5 ? 2 : 4
    }
  }
}

/** ANSI 文本 → HTML 片段（已转义，可用于 v-html）。 */
export function ansiToHtml(text) {
  if (!text) return ''
  const state = { fg: '', bg: '', bold: false }
  let html = ''
  let last = 0
  const push = (chunk) => {
    if (!chunk) return
    const safe = escapeHtml(chunk)
    const cls = classOf(state)
    html += cls ? `<span class="${cls}">${safe}</span>` : safe
  }
  ESC_RE.lastIndex = 0
  let m = ESC_RE.exec(text)
  while (m) {
    push(text.slice(last, m.index))
    if (m[2] === 'm') applySgr(state, m[1])  // 其余控制序列丢弃
    last = m.index + m[0].length
    m = ESC_RE.exec(text)
  }
  push(text.slice(last))
  return html
}

/** 去掉 ANSI 转义（用于按行匹配模块头、复制纯文本等）。 */
export function stripAnsi(text) {
  return String(text ?? '').replace(ESC_RE, '')
}
