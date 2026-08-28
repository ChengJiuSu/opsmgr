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
          <el-tag v-if="polling" type="info" size="small">实时刷新中…</el-tag>
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
        <el-alert v-if="task?.status === 'paused'" :type="pausedFailed ? 'error' : 'warning'" :closable="false" show-icon class="pause-bar">
          <template #title>
            <span class="pause-line">
              {{ pausedFailed ? '模块执行失败，任务流已暂停，等待人工处理' : '任务流已暂停，等待人工确认后继续' }}
              <span v-if="canExec" class="pause-actions">
                <template v-if="pausedFailed">
                  <el-button size="small" type="primary" :loading="confirming" @click="handleConfirm('retry')">重试失败模块</el-button>
                  <el-button size="small" type="warning" :loading="confirming" @click="handleConfirm('skip')">跳过并继续</el-button>
                </template>
                <el-button v-else size="small" type="primary" :loading="confirming" @click="handleConfirm('continue')">继续执行</el-button>
                <el-button size="small" type="danger" :loading="confirming" @click="handleConfirm('abort')">中止任务流</el-button>
              </span>
            </span>
          </template>
        </el-alert>
        <StepOutput
          :output="output"
          :steps-result="current?.steps_result || []"
          :follow="polling"
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
let stream = null   // WebSocket 日志流（不可用时为 null，走 pollTimer）

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
  stopFollow()
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
  current.value = log
  if (String(log.id) !== String(route.params.logId)) {
    router.replace({ name: 'TaskLog', params: { taskId, logId: log.id } })
  }
  const isLatest = logs.value[0]?.id === log.id
  const running = ['pending', 'running', 'paused'].includes(task.value?.status)
  if (isLatest && running) {
    // 最新一次且执行中：优先 WebSocket 推送，不可用回退轮询
    output.value = task.value?.output || '（执行中，等待输出…）'
    startFollow()
    return
  }
  stopFollow()
  const res = await getTaskLogDetail(taskId, log.id)
  current.value = res.data
  output.value = res.data?.output || '（暂无输出）'
}

// 实时刷新状态：outOffset = 轮询已拉取长度；hasRaw = 当前展示的是真实日志（非占位）
let outOffset = 0
let hasRaw = false

// ── 实时日志：WebSocket 优先，失败回退轮询 ────────────────
function startFollow() {
  stopFollow()
  polling.value = true
  hasRaw = false
  stream = openTaskLogStream(taskId, {
    onFull: (text) => {
      output.value = text || '（执行中，等待输出…）'
      hasRaw = !!text
    },
    onDelta: (chunk) => {
      if (hasRaw) output.value += chunk
      else { output.value = chunk; hasRaw = true }
    },
    onStatus: applyStatus,
    onFallback: () => {
      stream = null
      if (polling.value) startPolling()  // 通道不可用：退回轮询接口
    },
  })
}

function applyStatus(data) {
  task.value = {
    ...task.value,
    status: data.status,
    pause_type: data.pause_type,
    return_code: data.return_code,
    duration: data.duration,
  }
  if (data.status === 'success' || data.status === 'failed') {
    stopFollow()
    init() // 执行完成：刷新日志列表并定位最新记录
  }
}

function stopFollow() {
  stream?.close()
  stream = null
  stopPolling()
}

function startPolling() {
  stopPolling()
  polling.value = true
  outOffset = 0
  hasRaw = false
  pollTimer = setInterval(async () => {
    try {
      const res = await getTaskOutput(taskId, hasRaw ? outOffset : 0)
      const data = res.data || {}
      // output/offset/delta 属于日志流，不能并进任务对象（否则增量片段会污染 task.output）
      const { output: _o, offset: _f, delta: _d, ...taskFields } = data
      task.value = { ...task.value, ...taskFields }
      if (data.delta) {
        if (data.output) output.value += data.output   // 只回传新增部分，追加
      } else {
        output.value = data.output || '（执行中，等待输出…）'
      }
      outOffset = data.offset || 0
      hasRaw = hasRaw || !!data.output
      if (data.status === 'success' || data.status === 'failed') {
        stopPolling()
        init() // 执行完成：刷新日志列表并定位最新记录
      }
    } catch {
      stopPolling()
    }
  }, 1000)
}

function stopPolling() {
  polling.value = false
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
}

// 暂停处理：确认模块（继续/中止）或失败等待（重试/跳过/中止），状态由轮询自动刷新
const confirming = ref(false)
const pausedFailed = computed(() => task.value?.pause_type === 'failed')
const CONFIRM_DONE_MSG = {
  continue: '已确认，任务流继续执行',
  abort: '已发送中止指令',
  retry: '已发送重试指令，失败模块将重新执行',
  skip: '已发送跳过指令，任务流继续执行后续模块',
}
async function handleConfirm(action) {
  if (action === 'abort') {
    await ElMessageBox.confirm('确定中止任务流吗？后续模块将不再执行。', '警告', { type: 'warning' })
  } else if (action === 'skip') {
    await ElMessageBox.confirm('确定跳过失败模块吗？任务流将继续执行后续模块（本次执行仍记为失败）。', '提示', { type: 'warning' })
  }
  confirming.value = true
  try {
    await confirmTask(taskId, action)
    ElMessage.success(CONFIRM_DONE_MSG[action])
  } finally {
    confirming.value = false
  }
}

// ── 执行现场：inventory / 变量 / 命令归档（仅管理员）────────
const sceneVisible = ref(false)
const sceneLoading = ref(false)
const sceneFiles = ref([])
const sceneActive = ref('')
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
  sceneContent.value = res.data?.content || '（空文件）'
  sceneTruncated.value = !!res.data?.truncated
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
onUnmounted(stopFollow)
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
.scene-content pre { margin: 0; padding: 10px; background: #f5f7fa; border-radius: 4px; font-size: 12px; white-space: pre-wrap; word-break: break-all; }
</style>
