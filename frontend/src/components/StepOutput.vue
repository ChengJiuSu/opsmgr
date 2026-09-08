<template>
  <!-- 任务流日志：左侧模块列表，右侧为选中模块的执行日志（日志只按模块看，无「全部」视图） -->
  <div class="step-output">
    <div v-if="modules.length" class="flow-tree">
      <div
        v-for="mod in modules"
        :key="mod.index"
        class="tree-node"
        :class="{ active: current === mod.index }"
        @click="select(mod.index)"
      >
        <span class="node-dot" :class="`dot-${mod.status || 'pending'}`" />
        <span class="node-order">{{ mod.index }}</span>
        <span class="node-title" :title="mod.fullTitle">{{ mod.title }}</span>
        <span v-if="mod.parallel" class="node-parallel" title="并行组模块">∥</span>
        <el-icon v-if="mod.status === 'running'" class="is-loading node-loading"><Loading /></el-icon>
        <el-tag v-else-if="mod.status" size="small" :type="STATUS_TAG[mod.status] || 'info'" class="node-tag">
          {{ statusLabel(mod) }}
        </el-tag>
      </div>
    </div>

    <div class="log-pane">
      <div v-if="currentMeta" class="pane-header">
        <span class="pane-title">{{ currentMeta.fullTitle }}</span>
        <span v-if="currentMeta.duration != null" class="pane-meta">耗时 {{ currentMeta.duration }}s</span>
        <span v-if="currentMeta.return_code != null" class="pane-meta">rc={{ currentMeta.return_code }}</span>
        <span v-if="currentMeta.attempts > 1" class="pane-meta">已重试 {{ currentMeta.attempts - 1 }} 次</span>
      </div>
      <!-- ansible 彩色输出：ANSI 转义已转为已转义的 span（见 utils/ansi.js） -->
      <pre ref="paneRef" class="pane-body" v-html="currentHtml" />
    </div>
  </div>
</template>

<script setup>
import { ref, computed, nextTick, watch } from 'vue'
import { Loading } from '@element-plus/icons-vue'
import { ansiToHtml, stripAnsi } from '../utils/ansi'

const props = defineProps({
  output: { type: String, default: '' },
  // 执行日志的逐模块结果（steps_result）：提供完整模块列表与状态
  // （执行中即含 pending 的后续模块，由 runner 开跑时一次建好）
  stepsResult: { type: Array, default: () => [] },
  // 实时轮询中：自动选中最新模块并滚动到底部（用户手动点选后不再抢焦点）
  follow: { type: Boolean, default: false },
  // 当前执行记录 id：切换记录时重置选中（不重置的话会带着上一条记录选中的模块
  // 序号去看新记录，撞上没有该模块的记录就是一片「暂无输出」）
  logId: { type: [Number, String], default: null },
})

const STATUS_TAG = { success: 'success', failed: 'danger', skipped: 'info', pending: 'info' }
const STATUS_LABEL = { success: '成功', failed: '失败', skipped: '跳过', pending: '待执行' }
// 失败后被人工跳过的模块（任务流继续但本次执行仍记失败）
const statusLabel = (mod) =>
  mod.status === 'failed' && mod.skippedOver ? '失败·已跳过' : STATUS_LABEL[mod.status] || mod.status

// runner 输出的模块分隔头：═══ 模块 i/n：名称（类型 · 范围）═══
const HEADER_RE = /^═══ 模块 (\d+)\/(\d+)：(.+?)═══\s*$/
// steps_result 里发布的模块头是整行（带 ═══），标题区只要中间那段
const stripHeader = (line) => (line ? (stripAnsi(line).match(HEADER_RE)?.[3] || '').trim() : '')

const paneRef = ref()
// 当前选中的模块序号。执行中自动跟随（跟随结果也写进这里，见下面的 watch），
// 用户点选后 userPinned 置位，执行进度不再抢焦点
const selected = ref(null)
const userPinned = ref(false)

// ── 输出解析：按模块头拆分 ───────────────────────────────
const parsed = computed(() => {
  const sections = []
  const preamble = []
  let current = null
  for (const line of (props.output || '').split('\n')) {
    // 模块头由平台生成（无颜色码），仍按去色后的文本匹配以防夹带转义
    const m = stripAnsi(line).match(HEADER_RE)
    if (m) {
      if (current) sections.push(current)
      current = { index: Number(m[1]), header: m[3].trim(), lines: [] }
      continue
    }
    if (current) current.lines.push(line)
    else preamble.push(line)
  }
  if (current) sections.push(current)
  return {
    preamble: preamble.join('\n').trim(),
    sections: sections.map((s) => ({ ...s, body: s.lines.join('\n').trim() })),
  }
})
const sectionByIndex = computed(() => new Map(parsed.value.sections.map((s) => [s.index, s])))

// ── 模块树：steps_result 为准（含 skipped），实时执行中回退输出解析 ──
const modules = computed(() => {
  const secs = sectionByIndex.value
  if (props.stepsResult?.length) {
    return props.stepsResult.map((r, i) => ({
      index: i + 1,
      title: r.name || `模块 ${i + 1}`,
      // 模块头优先取日志里解析出的那行，其次取 steps_result 里发布的（模块刚开跑、
      // 日志还没到时也能显示类型与范围），最后退回模块名
      fullTitle: secs.get(i + 1)?.header || stripHeader(r.header) || r.name || `模块 ${i + 1}`,
      status: r.status,
      duration: r.duration,
      return_code: r.return_code,
      parallel: !!r.parallel,
      attempts: r.attempts || 1,
      skippedOver: !!r.skipped_over,
      section: secs.get(i + 1) || null,
    }))
  }
  // 执行中（尚无 steps_result）：已出现的模块，末个标记执行中
  return parsed.value.sections.map((s, i, arr) => ({
    index: s.index,
    title: s.header.split('（')[0] || `模块 ${s.index}`,
    fullTitle: s.header,
    status: props.follow && i === arr.length - 1 ? 'running' : null,
    duration: null,
    return_code: null,
    section: s,
  }))
})

// 自动选中的目标：从后往前找第一个「跑过或正在跑」的模块。执行中它就是当前模块
// （后面的都还 pending），执行完就是最后跑完的那个。
// 故意不去找 running：模块表来自多条通道（WebSocket 快照/推送、轮询、记录详情、
// 列表行），其中任何一处都可能送来一份「前面的模块还在 running」的旧快照，而任务流
// 只进不退，跟着那个 running 走就会把视图拽回前面的模块。
const AUTO_SKIP = ['pending', 'skipped']   // 没跑过的模块没有日志，不作为自动目标
const autoIndex = computed(() => {
  const last = [...modules.value].reverse().find((m) => m.status && !AUTO_SKIP.includes(m.status))
  return last?.index ?? null
})

// 实际展示的模块序号：选中的那个（只要还在列表里），否则自动目标，最后退回列表末尾。
// 兜底一律往后落：执行结束时这条记录会被重新拉一次完整日志，落回第一个模块等于把
// 用户正在看的那一屏冲掉——刚跑完的那个模块才是他在看的
const current = computed(() => {
  const list = modules.value
  if (selected.value != null && list.some((m) => m.index === selected.value)) return selected.value
  return autoIndex.value ?? list[list.length - 1]?.index ?? null
})

// 把自动目标**写进 selected**（不只是算出来）：执行结束时模块表里已经没有 running
// 项，纯计算的选中会随之飘走。写进去之后与手动点选同一套逻辑，停在原处。
// 只许往后、不许往前：往前一定是过期快照造成的（任务流只进不退）。
watch(autoIndex, (idx) => {
  if (idx == null || userPinned.value) return
  if (selected.value != null && idx <= selected.value) return
  selected.value = idx
}, { immediate: true })

const currentMeta = computed(() =>
  modules.value.find((m) => m.index === current.value) || null,
)

// 第一个模块头之前那段无主日志（实时窗口裁掉了头部、或缺口提示）该算在谁名下：
// 它是「第一个带头的模块」的前一个模块产出的；没有解析出任何模块头时（整段都是
// 某个模块的中段）算给正在执行的模块。不挂到任何模块上就等于看不见。
const leadOwner = computed(() => {
  if (!parsed.value.preamble) return null
  const list = modules.value
  const secs = parsed.value.sections
  if (!secs.length) {
    return (list.find((m) => m.status === 'running') || list[list.length - 1])?.index ?? null
  }
  const first = secs[0].index
  return list.some((m) => m.index === first - 1) ? first - 1 : first
})

const currentBody = computed(() => {
  const mod = currentMeta.value
  // 没有模块（旧记录、任务未配置模块、执行前的占位文案）：原样显示整段输出
  if (!mod) return props.output || '（暂无输出）'
  const lead = mod.index === leadOwner.value ? parsed.value.preamble : ''
  const body = mod.section?.body
    || (mod.status === 'skipped' ? '（该模块因前序失败被跳过，未执行）' : '')
  if (lead && body) return `${lead}\n\n${body}`
  // 还没有日志时的占位：执行中的模块说「等输出」，没轮到的说「等执行」
  return lead || body || (mod.status === 'running' ? '（执行中，等待输出…）' : '（等待执行…）')
})

const currentHtml = computed(() => ansiToHtml(currentBody.value))

function select(index) {
  selected.value = index
  userPinned.value = true
}

// 切换执行记录：交还自动选中（current 会落到执行中的模块、否则最后一个跑过的）
watch(() => props.logId, () => {
  selected.value = null
  userPinned.value = false
})

// 跟随模式：新输出到达自动选中正在执行的模块并滚到底（用户点选过则只滚动不抢选中）
watch(() => props.output, async () => {
  if (!props.follow) return
  // 未手动点选时 current 自己就跟着执行中的模块走，这里只负责滚到底
  await nextTick()
  if (paneRef.value) paneRef.value.scrollTop = paneRef.value.scrollHeight
})
</script>

<style scoped>
.step-output {
  display: flex;
  background: #1e1e1e;
  border-radius: 6px;
  overflow: hidden;
}

/* ── 左侧模块树 ── */
.flow-tree {
  width: 220px;
  flex-shrink: 0;
  overflow-y: auto;
  padding: 10px 0;
  border-right: 1px solid #333;
  background: #232323;
}
.tree-node {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 12px;
  cursor: pointer;
  color: #bdbdbd;
  font-size: 13px;
  user-select: none;
  white-space: nowrap;
}
.tree-node:hover { background: #2c2c2c; }
.tree-node.active { background: #094771; color: #fff; }
.node-dot {
  width: 8px; height: 8px; border-radius: 50%;
  flex-shrink: 0; background: #6b6b6b;
}
.dot-success { background: #67c23a; }
.dot-failed { background: #f56c6c; }
.dot-skipped { background: #6b6b6b; }
.dot-running { background: #e6a23c; }
.node-order { color: #6b6b6b; font-size: 12px; font-family: 'Courier New', monospace; }
.node-title { overflow: hidden; text-overflow: ellipsis; flex: 1; }
.node-tag { flex-shrink: 0; }
.node-loading { color: #e6a23c; }
.node-parallel { color: #e5c07b; font-weight: 700; flex-shrink: 0; }

/* ── 右侧日志区 ── */
.log-pane { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.pane-header {
  display: flex; align-items: center; gap: 14px;
  padding: 8px 16px;
  border-bottom: 1px solid #333;
  color: #e5c07b;
  font-family: 'Courier New', monospace;
  font-size: 13px; font-weight: 600;
  flex-shrink: 0;
}
.pane-meta { color: #6b6b6b; font-weight: 400; font-size: 12px; }
.pane-body {
  flex: 1;
  overflow-y: auto;
  margin: 0;
  padding: 12px 16px;
  color: #d4d4d4;
  font-family: 'Courier New', monospace;
  font-size: 13px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
}

/* ── ansible ANSI 颜色（v-html 生成的节点，需 :deep 穿透 scoped）── */
.pane-body :deep(.ansi-bold) { font-weight: 700; }
.pane-body :deep(.ansi-fg-black) { color: #6b6b6b; }
.pane-body :deep(.ansi-fg-red) { color: #f07178; }
.pane-body :deep(.ansi-fg-green) { color: #7ec699; }
.pane-body :deep(.ansi-fg-yellow) { color: #e2c08d; }
.pane-body :deep(.ansi-fg-blue) { color: #6fb3e0; }
.pane-body :deep(.ansi-fg-magenta) { color: #d08fd0; }
.pane-body :deep(.ansi-fg-cyan) { color: #6cc4c4; }
.pane-body :deep(.ansi-fg-white) { color: #e6e6e6; }
.pane-body :deep(.ansi-fg-bright-black) { color: #8a8a8a; }
.pane-body :deep(.ansi-fg-bright-red) { color: #ff7b80; }
.pane-body :deep(.ansi-fg-bright-green) { color: #93e6ac; }
.pane-body :deep(.ansi-fg-bright-yellow) { color: #f2d18b; }
.pane-body :deep(.ansi-fg-bright-blue) { color: #8ac6f2; }
.pane-body :deep(.ansi-fg-bright-magenta) { color: #e6a3e6; }
.pane-body :deep(.ansi-fg-bright-cyan) { color: #87dada; }
.pane-body :deep(.ansi-fg-bright-white) { color: #ffffff; }
.pane-body :deep(.ansi-bg-red) { background: #5a2226; }
.pane-body :deep(.ansi-bg-green) { background: #24452f; }
.pane-body :deep(.ansi-bg-yellow) { background: #4d3f22; }
.pane-body :deep(.ansi-bg-blue) { background: #22364d; }
.pane-body :deep(.ansi-bg-magenta) { background: #43264a; }
.pane-body :deep(.ansi-bg-cyan) { background: #1f4444; }
.pane-body :deep(.ansi-bg-white) { background: #4a4a4a; }
.pane-body :deep(.ansi-bg-black) { background: #2a2a2a; }
</style>
