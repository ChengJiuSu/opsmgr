<template>
  <!-- 任务流日志：左侧模块树（任务流 → 模块），右侧为选中模块的执行日志 -->
  <div class="step-output">
    <div v-if="modules.length" class="flow-tree">
      <div
        class="tree-node tree-root"
        :class="{ active: selected === ALL }"
        @click="select(ALL)"
      >
        <span class="node-dot root-dot" />
        <span class="node-title">任务流（全部日志）</span>
      </div>
      <div
        v-for="mod in modules"
        :key="mod.index"
        class="tree-node tree-leaf"
        :class="{ active: selected === mod.index }"
        @click="select(mod.index)"
      >
        <span class="node-dot" :class="`dot-${mod.status || 'pending'}`" />
        <span class="node-order">{{ mod.index }}</span>
        <span class="node-title" :title="mod.fullTitle">{{ mod.title }}</span>
        <span v-if="mod.parallel" class="node-parallel" title="并行组模块">∥</span>
        <el-icon v-if="mod.status === 'running'" class="is-loading node-loading"><Loading /></el-icon>
        <el-tag v-else-if="mod.status" size="small" :type="STATUS_TAG[mod.status] || 'info'" class="node-tag">
          {{ STATUS_LABEL[mod.status] || mod.status }}
        </el-tag>
      </div>
    </div>

    <div class="log-pane">
      <div v-if="currentMeta" class="pane-header">
        <span class="pane-title">{{ currentMeta.fullTitle }}</span>
        <span v-if="currentMeta.duration != null" class="pane-meta">耗时 {{ currentMeta.duration }}s</span>
        <span v-if="currentMeta.return_code != null" class="pane-meta">rc={{ currentMeta.return_code }}</span>
      </div>
      <pre ref="paneRef" class="pane-body">{{ currentBody }}</pre>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, nextTick, watch } from 'vue'
import { Loading } from '@element-plus/icons-vue'

const props = defineProps({
  output: { type: String, default: '' },
  // 执行日志的逐模块结果（steps_result）：提供完整模块列表与状态（含 skipped）
  stepsResult: { type: Array, default: () => [] },
  // 实时轮询中：自动选中最新模块并滚动到底部（用户手动点选后不再抢焦点）
  follow: { type: Boolean, default: false },
})

const ALL = '__all__'
const STATUS_TAG = { success: 'success', failed: 'danger', skipped: 'info' }
const STATUS_LABEL = { success: '成功', failed: '失败', skipped: '跳过' }

// runner 输出的模块分隔头：═══ 模块 i/n：名称（类型 · 范围）═══
const HEADER_RE = /^═══ 模块 (\d+)\/(\d+)：(.+?)═══\s*$/

const paneRef = ref()
const selected = ref(ALL)
// 用户手动点选后，follow 不再自动切换选中模块
const userPinned = ref(false)

// ── 输出解析：按模块头拆分 ───────────────────────────────
const parsed = computed(() => {
  const sections = []
  const preamble = []
  let current = null
  for (const line of (props.output || '').split('\n')) {
    const m = line.match(HEADER_RE)
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
      fullTitle: secs.get(i + 1)?.header || r.name || `模块 ${i + 1}`,
      status: r.status,
      duration: r.duration,
      return_code: r.return_code,
      parallel: !!r.parallel,
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

const currentMeta = computed(() =>
  selected.value === ALL ? null : modules.value.find((m) => m.index === selected.value) || null,
)

const currentBody = computed(() => {
  if (selected.value === ALL) return props.output || '（暂无输出）'
  const mod = currentMeta.value
  if (!mod) return '（暂无输出）'
  if (mod.section) return mod.section.body || '（暂无输出）'
  if (mod.status === 'skipped') return '（该模块因前序失败被跳过，未执行）'
  return '（等待执行…）'
})

function select(key) {
  selected.value = key
  userPinned.value = true
}

// 跟随模式：新输出到达自动选中最新模块并滚到底（用户点选过则只滚动不抢选中）
watch(() => props.output, async () => {
  if (!props.follow) return
  if (!userPinned.value) {
    const secs = parsed.value.sections
    if (secs.length) selected.value = secs[secs.length - 1].index
  }
  await nextTick()
  if (paneRef.value) paneRef.value.scrollTop = paneRef.value.scrollHeight
})

// 切换日志记录：重置为全部视图
watch(() => props.stepsResult, () => {
  if (!props.follow) {
    selected.value = ALL
    userPinned.value = false
  }
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
.tree-leaf {
  margin-left: 14px;
  border-left: 1px solid #444;  /* 任务流树的连接线 */
  padding-left: 16px;
}
.node-dot {
  width: 8px; height: 8px; border-radius: 50%;
  flex-shrink: 0; background: #6b6b6b;
}
.root-dot { background: #409eff; }
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
</style>
