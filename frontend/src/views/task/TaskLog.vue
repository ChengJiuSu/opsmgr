<template>
  <!-- 独立执行日志页：URL 含任务与日志 id，可直接分享定位到某次执行 -->
  <el-card shadow="never" class="log-page">
    <template #header>
      <div class="page-header">
        <span class="header-left">
          <el-button text :icon="Back" @click="router.push({ name: 'TaskExecutions' })">任务列表</el-button>
          <el-divider direction="vertical" />
          <span class="task-name">执行日志 — {{ task?.name || `任务 #${taskId}` }}</span>
          <el-tag v-if="task" size="small" :type="statusType(task.status)" style="margin-left:8px">
            <el-icon v-if="task.status === 'running'" class="is-loading"><Loading /></el-icon>
            {{ task.status_display || task.status }}
          </el-tag>
        </span>
        <span class="header-right">
          <el-tag v-if="following" type="info" size="small">实时刷新中…</el-tag>
          <el-button size="small" :icon="Refresh" @click="init">刷新</el-button>
          <el-button size="small" type="primary" :icon="CopyDocument" @click="copyLink">复制链接</el-button>
        </span>
      </div>
    </template>

    <div v-loading="loading" class="log-body">
      <!-- 执行记录列表：点击切换，URL 同步替换日志 id -->
      <div class="log-list">
        <el-table
          :data="logs" size="small" highlight-current-row
          :row-class-name="() => 'log-row'"
          @current-change="selectLog"
          ref="logTableRef"
        >
          <el-table-column label="执行时间" min-width="130">
            <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
          </el-table-column>
          <el-table-column prop="triggered_by_name" label="触发人" width="80" />
          <el-table-column label="模块" width="70">
            <template #default="{ row }">
              <span :class="{ danger: stepsFailed(row) }">{{ stepsSummary(row) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="80">
            <template #default="{ row }">
              <el-tag size="small" :type="statusType(row.status)">{{ row.status_display }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="耗时" width="70">
            <template #default="{ row }">{{ row.duration != null ? row.duration + 's' : '-' }}</template>
          </el-table-column>
        </el-table>
      </div>

      <div class="log-output">
        <div class="output-meta">
          <span v-if="current">返回码：{{ current.return_code ?? '-' }}</span>
          <span v-if="currentServers" class="muted">{{ currentServers }}</span>
          <!-- 执行现场归档含变量值，仅管理员可读 -->
          <el-button v-if="canScene && current?.id" link type="primary" size="small" @click="openScene">
            执行现场
          </el-button>
        </div>
        <!-- 暂停处理操作条：暂停确认模块（继续/中止）或模块失败等待（重试/跳过/中止） -->
        <el-alert v-if="showPauseBar" :type="pausedFailed ? 'error' : 'warning'" :closable="false" show-icon class="pause-bar">
          <template #title>
            <span class="pause-line">
              {{ pauseTitle(task) }}
              <span v-if="canExec" class="pause-actions">
                <template v-if="pausedFailed">
                  <el-button size="small" type="primary" :loading="confirming" @click="handleConfirm('retry')">重试失败模块</el-button>
                  <el-button size="small" type="warning" :loading="confirming" @click="handleConfirm('skip')">跳过并继续</el-button>
                </template>
                <el-button v-else size="small" type="primary" :loading="confirming" @click="handleConfirm('continue')">继续执行</el-button>
                <el-button size="small" type="danger" :loading="confirming" @click="handleConfirm('abort')">{{ pausedFailed ? '终止' : '中止任务流' }}</el-button>
              </span>
            </span>
          </template>
        </el-alert>
        <StepOutput
          :output="output"
          :steps-result="current?.steps_result || []"
          :follow="following"
          :log-id="current?.id"
          class="output-host"
        />
      </div>
    </div>

    <!-- 执行现场：本次执行各模块实际用的 inventory / 变量 / 命令，供 debug 复现 -->
    <el-dialog v-model="sceneVisible" title="执行现场" width="900px" top="6vh">
      <div v-loading="sceneLoading" class="scene-body">
        <template v-if="sceneFiles.length">
          <el-menu :default-active="sceneActive" class="scene-menu" @select="loadSceneFile">
            <el-menu-item v-for="f in sceneFiles" :key="f.path" :index="f.path">
              <span class="scene-path">{{ f.path }}</span>
              <span class="muted scene-size">{{ f.size }}B</span>
            </el-menu-item>
          </el-menu>
          <div class="scene-content">
            <div v-if="sceneContent" class="scene-bar">
              <span class="muted scene-path">{{ sceneActive }}</span>
              <el-button size="small" :icon="CopyDocument" :disabled="sceneContent === EMPTY_SCENE"
                         @click="copyScene">复制</el-button>
            </div>
            <pre v-if="sceneContent">{{ sceneContent }}</pre>
            <el-empty v-else description="选择左侧文件查看内容" :image-size="60" />
            <el-alert v-if="sceneTruncated" type="warning" :closable="false" show-icon
                      title="内容过长已截断，完整文件见服务器归档目录" />
          </div>
        </template>
        <el-empty v-else-if="!sceneLoading" :image-size="80"
                  description="无执行现场归档（该次执行早于本功能上线，或归档已过保留期被清理）" />
      </div>
    </el-dialog>
  </el-card>
</template>

<script setup>
import { ref, computed, nextTick, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Back, CopyDocument, Loading, Refresh } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { getTask, getTaskLogs, getTaskLogDetail, getTaskLogScene, getTaskOutput, confirmTask } from '../../api/tasks'
import { useAuthStore } from '../../stores/auth'
import { openTaskLogStream } from '../../utils/taskLogStream'
import { createLogBuffer, applyFull, applyDelta, resetBuffer } from '../../utils/taskLogBuffer'
import { awaitingFailure, pauseTitle, CONFIRM_DONE_MSG } from '../../utils/taskPause'
import StepOutput from '../../components/StepOutput.vue'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()
// 现场信息与执行日志同档（exec read）：归档里敏感变量已打码、inventory 无凭据，
// 且「日志文件」页读的就是同一批文件，抬到写档只是个换个入口就绕过的假门槛。
// 处理暂停看能力位
const canScene = computed(() => authStore.can('exec'))
const canExec = computed(() => authStore.capability('can_execute'))
const taskId = Number(route.params.taskId)

const loading = ref(false)
const task = ref(null)
const logs = ref([])
const current = ref(null)
const output = ref('')
const polling = ref(false)
const logTableRef = ref()
let pollTimer = null
let listTimer = null
let resyncTimer = null
let stream = null   // WebSocket 日志流（不可用时为 null，走 pollTimer）
let lastFrameAt = 0            // 上次收到日志帧的时刻（推送静默兜底用）
const RESYNC_QUIET_MS = 15000  // 静默这么久就按偏移补拉一次
let selectSeq = 0   // 选中序号：异步取详情返回时用它丢弃已被覆盖的旧请求
// 正在看「执行中」那条记录：看历史记录时通道仍开着（状态/模块表照收，暂停操作条
// 不会因为翻了一眼历史就消失），但日志帧不再往展示缓冲里写
const liveTail = ref(false)
const following = computed(() => polling.value && liveTail.value)
// 实时日志缓冲：按绝对偏移把窗口帧拼成越来越长的完整日志（见 utils/taskLogBuffer）
const buf = createLogBuffer()

const statusType = (s) => ({ pending: 'info', running: 'warning', paused: 'primary', success: 'success', failed: 'danger' }[s] || 'info')
const formatTime = (t) => (t ? t.slice(0, 19).replace('T', ' ') : '')

const stepsSummary = (log) => {
  const steps = log.steps_result || []
  if (!steps.length) return '-'
  const ok = steps.filter((s) => s.status === 'success').length
  return `${ok}/${steps.length}`
}
const stepsFailed = (log) => (log.steps_result || []).some((s) => s.status === 'failed')

const currentServers = computed(() => {
  const snap = current.value?.servers_snapshot
  if (!snap?.length) return ''
  const names = snap.slice(0, 5).map((s) => s.name).join('、')
  return `全局主机：${names}${snap.length > 5 ? ` 等 ${snap.length} 台` : ''}`
})

async function init() {
  loading.value = true
  liveTail.value = false
  stopFollow()
  stopListTimer()
  try {
    const [t, l] = await Promise.all([getTask(taskId), getTaskLogs(taskId)])
    task.value = t.data
    logs.value = l.data || []
    // URL 带日志 id 则定位该条，否则选最新一条（并把 id 补进 URL）
    const targetId = Number(route.params.logId)
    const target = logs.value.find((x) => x.id === targetId) || logs.value[0]
    if (target) {
      await nextTick()
      logTableRef.value?.setCurrentRow(target) // 触发 selectLog
    } else {
      output.value = '（该任务暂无执行记录）'
    }
  } finally {
    loading.value = false
  }
}

async function selectLog(log) {
  if (!log) return
  const seq = ++selectSeq
  current.value = log
  if (String(log.id) !== String(route.params.logId)) {
    router.replace({ name: 'TaskLog', params: { taskId, logId: log.id } })
  }
  // 「是否执行中」看这条记录自己的状态，不看任务快照：任务快照可能是上一次执行
  // 留下的终态，据此判断会把执行中的记录当历史记录去读详情，而执行中的详情里
  // 只有一份还没回填的预览（页面上就是「暂无输出」，刷新才好）
  const isLatest = logs.value[0]?.id === log.id
  const live = ['pending', 'running', 'paused'].includes(
    log.status || task.value?.status,
  )
  if (isLatest && live) {
    // 最新一次且执行中：优先 WebSocket 推送，不可用回退轮询（重连以取回完整快照）
    output.value = '（执行中，等待输出…）'
    liveTail.value = true
    startFollow()
    startListTimer()
    return
  }
  liveTail.value = false
  const res = await getTaskLogDetail(taskId, log.id)
  if (seq !== selectSeq) return   // 期间已切到别的记录：丢弃这次结果
  current.value = res.data
  output.value = res.data?.output || '（暂无输出）'
}

// ── 实时日志：WebSocket 优先，失败回退轮询 ────────────────
function render() {
  output.value = buf.text || '（执行中，等待输出…）'
}

function applySteps(steps) {
  // 执行中的模块表（含后续 pending 模块）随推送更新，任务流树据此保持完整
  if (current.value) current.value = { ...current.value, steps_result: steps || [] }
}

function startFollow() {
  stopFollow()
  polling.value = true
  resetBuffer(buf)
  lastFrameAt = Date.now()
  stream = openTaskLogStream(taskId, {
    onFull: (frame) => { lastFrameAt = Date.now(); if (liveTail.value) { applyFull(buf, frame); render() } },
    onDelta: (frame) => { lastFrameAt = Date.now(); if (liveTail.value) { applyDelta(buf, frame); render() } },
    onSteps: applySteps,
    onStatus: applyStatus,
    onFallback: () => {
      stream = null
      if (polling.value) startPolling()  // 通道不可用：退回轮询接口
    },
  })
  startResyncTimer()
}

// 推送静默兜底：连接活着（pong 正常）但一直收不到日志帧时，按绝对偏移补拉一次。
// 推送链路（channel layer / Redis）出问题时看门狗察觉不到——它把 pong 也算活动，
// 于是页面会一直停在旧日志上，而任务其实在跑。补拉是幂等的（偏移对齐）。
function startResyncTimer() {
  stopResyncTimer()
  resyncTimer = setInterval(async () => {
    if (!stream || Date.now() - lastFrameAt < RESYNC_QUIET_MS) return
    lastFrameAt = Date.now()
    try {
      // 状态帧可能一起丢了，所以补拉也要负责收尾
      if (await pollOnce()) finish()
    } catch { /* 下一轮再试 */ }
  }, 5000)
}

function stopResyncTimer() {
  if (resyncTimer) { clearInterval(resyncTimer); resyncTimer = null }
}

function applyStatus(data) {
  task.value = {
    ...task.value,
    status: data.status,
    pause_type: data.pause_type,
    resume_deadline: data.resume_deadline,
    return_code: data.return_code,
    duration: data.duration,
  }
  if (data.status === 'success' || data.status === 'failed') finish()
}

// 执行结束的收尾：只把当前记录换成落盘的完整日志，并就地刷新列表各行的状态/耗时。
// **不能走 init()**：那会重置表格数据、重新 setCurrentRow、重新选中记录，页面看着
// 像刷新了一次，日志区还会退回第一个模块——而用户正在看的是刚跑完的那个模块。
async function finish() {
  stopFollow()
  stopListTimer()
  const logId = current.value?.id
  const [list, detail] = await Promise.all([
    getTaskLogs(taskId),
    logId ? getTaskLogDetail(taskId, logId) : Promise.resolve(null),
  ])
  // 就地更新行对象（保持身份）：表格的当前行不变，也就不会触发重新选中
  const byId = new Map((list.data || []).map((r) => [r.id, r]))
  logs.value.forEach((row) => Object.assign(row, byId.get(row.id) || {}))
  if (detail?.data) {
    // 合并而不是整份替换：保持 current 的对象身份，收尾这一拍就不会有 prop 身份
    // 变化传到日志组件（它按 logId 判断「换了执行记录」，一变就重置选中的模块）
    Object.assign(current.value, detail.data)
    output.value = detail.data.output || '（暂无输出）'
  }
}

function stopFollow() {
  stream?.close()
  stream = null
  stopResyncTimer()
  stopPolling()
}

/** 拉一次输出（轮询回退与推送静默兜底共用）。返回是否已到终态。 */
async function pollOnce() {
  const res = await getTaskOutput(taskId, buf.offset)
  const data = res.data || {}
  // 日志流字段不能并进任务对象（否则增量片段会污染 task.output）
  const { output: _o, head: _h, start: _s, offset: _f, delta: _d,
          steps_result: steps, ...taskFields } = data
  task.value = { ...task.value, ...taskFields }
  if (liveTail.value) {
    if (data.delta) applyDelta(buf, data)
    else applyFull(buf, data)
    render()
  }
  if (steps?.length) applySteps(steps)   // 空数组不覆盖（[] 是真值，会抹掉模块表）
  return data.status === 'success' || data.status === 'failed'
}

function startPolling() {
  stopPolling()
  polling.value = true
  resetBuffer(buf)
  pollTimer = setInterval(async () => {
    try {
      if (await pollOnce()) {
        stopPolling()
        finish() // 执行完成：换成完整日志（不重置选中，见 finish）
      }
    } catch {
      stopPolling()
    }
  }, 1000)
}

// 执行中定时刷新记录列表：本页停留期间又发起了新执行时，列表与「执行中」
// 那条记录才会自己冒出来（否则要手动刷新）
function startListTimer() {
  stopListTimer()
  listTimer = setInterval(async () => {
    const res = await getTaskLogs(taskId)
    const rows = res.data || []
    if (rows[0] && rows[0].id !== logs.value[0]?.id) {
      init()   // 又发起了一次执行：整页重来（定位到新记录并重新跟随）
      return
    }
    // 就地更新（保持行对象身份）：表格的当前选中行与滚动位置都不受影响
    const byId = new Map(rows.map((r) => [r.id, r]))
    logs.value.forEach((row) => Object.assign(row, byId.get(row.id) || {}))
  }, 5000)
}

function stopListTimer() {
  if (listTimer) { clearInterval(listTimer); listTimer = null }
}

function stopPolling() {
  polling.value = false
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
}

// 暂停处理：确认模块（继续/中止）或失败待处理（重试/跳过/终止）
const confirming = ref(false)
const pausedFailed = computed(() => awaitingFailure(task.value))
const showPauseBar = computed(() => task.value?.status === 'paused' || pausedFailed.value)
async function handleConfirm(action) {
  if (action === 'abort') {
    await ElMessageBox.confirm('确定中止任务流吗？后续模块将不再执行。', '警告', { type: 'warning' })
  } else if (action === 'skip') {
    await ElMessageBox.confirm('确定跳过失败模块吗？任务流将继续执行后续模块（本次执行仍记为失败）。', '提示', { type: 'warning' })
  }
  const wasFailed = pausedFailed.value
  confirming.value = true
  try {
    await confirmTask(taskId, action)
    ElMessage.success(CONFIRM_DONE_MSG[action])
    // 暂停确认的状态由实时通道刷新；失败待处理时执行早已收尾、通道已断，要自己接上
    if (wasFailed) {
      const resumed = action !== 'abort'
      task.value = {
        ...task.value, pause_type: '', resume_deadline: null,
        ...(resumed ? { status: 'pending' } : {}),
      }
      if (resumed) {
        // 续跑沿用同一条执行记录：重新跟随实时输出（日志接着上次往后写）
        liveTail.value = true
        startFollow()
        startListTimer()
      } else {
        finish()   // 终止：换成追加了中止说明的完整日志，模块表里后续模块记跳过
      }
    }
  } finally {
    confirming.value = false
  }
}

// ── 执行现场：inventory / 变量 / 命令归档（仅管理员）────────
const sceneVisible = ref(false)
const sceneLoading = ref(false)
const sceneFiles = ref([])
const sceneActive = ref('')
const EMPTY_SCENE = '（空文件）'   // 空文件的占位文案，占位时不给复制
const sceneContent = ref('')
const sceneTruncated = ref(false)

async function openScene() {
  sceneVisible.value = true
  sceneLoading.value = true
  sceneFiles.value = []
  sceneActive.value = ''
  sceneContent.value = ''
  sceneTruncated.value = false
  try {
    const res = await getTaskLogScene(taskId, current.value.id)
    sceneFiles.value = res.data?.files || []
    // 默认展开第一个文件，省一次点击（多为 step1/direct/command.txt）
    if (sceneFiles.value.length) await loadSceneFile(sceneFiles.value[0].path)
  } finally {
    sceneLoading.value = false
  }
}

async function loadSceneFile(path) {
  sceneActive.value = path
  const res = await getTaskLogScene(taskId, current.value.id, path)
  sceneContent.value = res.data?.content || EMPTY_SCENE
  sceneTruncated.value = !!res.data?.truncated
}

// 复制当前现场文件内容；截断时如实提示复制的只是截断后的部分
async function copyScene() {
  const text = sceneContent.value
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    // 非 https/localhost 环境 clipboard API 不可用，退回 execCommand
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(ta)
    if (!ok) {
      ElMessage.error('复制失败，请手动选中内容复制')
      return
    }
  }
  ElMessage.success(sceneTruncated.value ? '已复制（内容过长已截断）' : `已复制 ${sceneActive.value}`)
}

async function copyLink() {
  const url = window.location.href
  try {
    await navigator.clipboard.writeText(url)
    ElMessage.success('链接已复制')
  } catch {
    // 非 https/localhost 环境 clipboard API 不可用，退回手动复制
    ElMessage.info(`请手动复制：${url}`)
  }
}

onMounted(init)
onUnmounted(() => { stopFollow(); stopListTimer() })   // stopFollow 连带停补拉定时器
</script>

<style scoped>
.page-header { display: flex; justify-content: space-between; align-items: center; }
.header-left { display: flex; align-items: center; }
.header-right { display: flex; align-items: center; gap: 10px; }
.task-name { font-weight: 600; font-size: 15px; }
.muted { color: #909399; }
.danger { color: #f56c6c; }
.log-body { display: flex; gap: 16px; }
/* 执行记录列表占 1/3，详细日志占 2/3 */
.log-list { width: 33.33%; min-width: 300px; flex-shrink: 0; }
.log-list :deep(.log-row) { cursor: pointer; }
.log-output { flex: 1; min-width: 0; }
.output-meta { display: flex; gap: 16px; align-items: center; margin-bottom: 8px; color: #606266; font-size: 13px; min-height: 20px; }
.pause-bar { margin-bottom: 8px; }
.pause-line { display: flex; align-items: center; gap: 12px; }
.pause-actions { display: inline-flex; gap: 8px; }
/* 独立页输出区高度：占满视口剩余高度（视觉样式在 StepOutput 组件内） */
.output-host { height: max(480px, calc(100vh - 260px)); }
/* 执行现场弹窗：左文件树右内容 */
.scene-body { display: flex; gap: 12px; min-height: 320px; }
.scene-menu { width: 280px; flex-shrink: 0; border-right: 1px solid #ebeef5; }
.scene-menu :deep(.el-menu-item) { height: 34px; line-height: 34px; font-size: 12px; display: flex; justify-content: space-between; }
.scene-path { overflow: hidden; text-overflow: ellipsis; }
.scene-size { margin-left: 8px; flex-shrink: 0; }
.scene-content { flex: 1; min-width: 0; max-height: 60vh; overflow: auto; }
.scene-bar { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 6px; }
.scene-bar .scene-path { font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.scene-content pre { margin: 0; padding: 10px; background: #f5f7fa; border-radius: 4px; font-size: 12px; white-space: pre-wrap; word-break: break-all; }
</style>
