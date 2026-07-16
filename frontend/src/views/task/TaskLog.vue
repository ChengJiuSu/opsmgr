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
        </div>
        <pre ref="outputRef" class="output-box">{{ output || '（暂无输出）' }}</pre>
      </div>
    </div>
  </el-card>
</template>

<script setup>
import { ref, computed, nextTick, watch, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Back, CopyDocument, Loading, Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { getTask, getTaskLogs, getTaskLogDetail, getTaskOutput } from '../../api/tasks'

const route = useRoute()
const router = useRouter()
const taskId = Number(route.params.taskId)

const loading = ref(false)
const task = ref(null)
const logs = ref([])
const current = ref(null)
const output = ref('')
const polling = ref(false)
const outputRef = ref()
const logTableRef = ref()
let pollTimer = null

const statusType = (s) => ({ pending: 'info', running: 'warning', success: 'success', failed: 'danger' }[s] || 'info')
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

// 实时轮询期间输出更新后自动滚到底部
watch(output, async () => {
  if (!polling.value) return
  await nextTick()
  if (outputRef.value) outputRef.value.scrollTop = outputRef.value.scrollHeight
})

async function init() {
  loading.value = true
  stopPolling()
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
  const running = ['pending', 'running'].includes(task.value?.status)
  if (isLatest && running) {
    // 最新一次且执行中：输出走轻量轮询实时刷新
    output.value = task.value?.output || '（执行中，等待输出…）'
    startPolling()
    return
  }
  stopPolling()
  const res = await getTaskLogDetail(taskId, log.id)
  current.value = res.data
  output.value = res.data?.output || '（暂无输出）'
}

function startPolling() {
  stopPolling()
  polling.value = true
  pollTimer = setInterval(async () => {
    try {
      const res = await getTaskOutput(taskId)
      task.value = { ...task.value, ...res.data }
      output.value = res.data.output || '（执行中，等待输出…）'
      if (res.data.status === 'success' || res.data.status === 'failed') {
        stopPolling()
        init() // 执行完成：刷新日志列表并定位最新记录
      }
    } catch {
      stopPolling()
    }
  }, 2000)
}

function stopPolling() {
  polling.value = false
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
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
onUnmounted(stopPolling)
</script>

<style scoped>
.page-header { display: flex; justify-content: space-between; align-items: center; }
.header-left { display: flex; align-items: center; }
.header-right { display: flex; align-items: center; gap: 10px; }
.task-name { font-weight: 600; font-size: 15px; }
.muted { color: #909399; }
.danger { color: #f56c6c; }
.log-body { display: flex; gap: 16px; }
.log-list { width: 420px; flex-shrink: 0; }
.log-list :deep(.log-row) { cursor: pointer; }
.log-output { flex: 1; min-width: 0; }
.output-meta { display: flex; gap: 16px; align-items: center; margin-bottom: 8px; color: #606266; font-size: 13px; min-height: 20px; }
.output-box {
  background: #1e1e1e;
  color: #d4d4d4;
  padding: 16px;
  border-radius: 6px;
  font-family: 'Courier New', monospace;
  font-size: 13px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
  /* 独立页：输出区占满视口剩余高度 */
  height: max(480px, calc(100vh - 260px));
  overflow-y: auto;
  margin: 0;
}
</style>
