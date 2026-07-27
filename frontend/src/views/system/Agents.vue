<template>
  <el-card shadow="never">
    <div class="toolbar">
      <span class="hint">
        NAT 出口执行代理：agent 主动外连本平台（/ws/agent/），心跳超过 90 秒判定离线。
        令牌仅在创建 / 重置时展示一次。
      </span>
      <div>
        <el-button :icon="Refresh" @click="fetchData">刷新</el-button>
        <el-button type="primary" :icon="Plus" @click="openDialog()">新增 Agent</el-button>
      </div>
    </div>

    <el-table v-loading="loading" :data="tableData" stripe>
      <el-table-column prop="name" label="名称" min-width="130" />
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tag size="small" :type="row.enabled ? (row.status === 'online' ? 'success' : 'danger') : 'info'">
            {{ row.enabled ? (row.status === 'online' ? '在线' : '离线') : '已禁用' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="version" label="版本" width="90">
        <template #default="{ row }">{{ row.version || '—' }}</template>
      </el-table-column>
      <el-table-column label="最近心跳" width="160">
        <template #default="{ row }">
          <span :class="{ muted: !row.last_heartbeat }">{{ formatTime(row.last_heartbeat) || '从未' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="运行任务" width="90">
        <template #default="{ row }">{{ row.running_jobs }}</template>
      </el-table-column>
      <el-table-column label="负载 / 磁盘空闲" width="140">
        <template #default="{ row }">
          <template v-if="row.meta?.load1 != null">{{ row.meta.load1 }} / {{ row.meta.disk_free_pct }}%</template>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column prop="remark" label="备注" min-width="140" show-overflow-tooltip>
        <template #default="{ row }">{{ row.remark || '—' }}</template>
      </el-table-column>
      <el-table-column label="操作" width="300" fixed="right">
        <template #default="{ row }">
          <el-button size="small" :icon="Edit" @click="openDialog(row)">编辑</el-button>
          <el-button size="small" @click="openJobs(row)">任务单元</el-button>
          <el-button size="small" type="warning" @click="handleRegenToken(row)">重置令牌</el-button>
          <el-button size="small" :type="row.enabled ? 'info' : 'success'" @click="handleToggle(row)">
            {{ row.enabled ? '禁用' : '启用' }}
          </el-button>
          <el-button size="small" type="danger" :icon="Delete" @click="handleDelete(row)" />
        </template>
      </el-table-column>
    </el-table>
  </el-card>

  <!-- 新增 / 编辑 -->
  <el-dialog v-model="dialogVisible" :title="editRow ? '编辑 Agent' : '新增 Agent'" width="460px">
    <el-form ref="formRef" :model="form" :rules="rules" label-width="70px">
      <el-form-item label="名称" prop="name">
        <el-input v-model="form.name" placeholder="如 nat-gw-sh01" />
      </el-form-item>
      <el-form-item label="备注">
        <el-input v-model="form.remark" type="textarea" :rows="2" placeholder="如所属网络 / 机房" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="dialogVisible = false">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="handleSubmit">确定</el-button>
    </template>
  </el-dialog>

  <!-- 一次性令牌展示 -->
  <el-dialog v-model="tokenVisible" title="Agent 令牌（仅展示一次）" width="560px" :close-on-click-modal="false">
    <el-alert type="warning" :closable="false" show-icon style="margin-bottom:12px">
      请立即复制并写入 agent 主机 /etc/ops-agent/config.yaml 的 token 字段，关闭后不再展示。
    </el-alert>
    <el-input :model-value="oneTimeToken" readonly>
      <template #append>
        <el-button @click="copyToken">复制</el-button>
      </template>
    </el-input>
    <template #footer>
      <el-button type="primary" @click="tokenVisible = false">我已保存</el-button>
    </template>
  </el-dialog>

  <!-- 任务单元（排障） -->
  <el-dialog v-model="jobsVisible" :title="`任务单元 — ${jobsAgent?.name || ''}（最近 50 条）`" width="760px">
    <el-table v-loading="jobsLoading" :data="jobs" size="small" stripe max-height="420">
      <el-table-column prop="job_id" label="单元 ID" width="120" show-overflow-tooltip />
      <el-table-column label="任务流/模块" width="110">
        <template #default="{ row }">
          <router-link v-if="row.task_id" :to="`/task/executions/${row.task_id}/logs`" class="link">
            #{{ row.task_id }} / {{ row.step_order ?? '—' }}
          </router-link>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tag size="small" :type="jobStatusType(row.status)">{{ jobStatusLabel(row.status) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="return_code" label="rc" width="60">
        <template #default="{ row }">{{ row.return_code ?? '—' }}</template>
      </el-table-column>
      <el-table-column prop="error" label="通道错误" min-width="160" show-overflow-tooltip>
        <template #default="{ row }">{{ row.error || '—' }}</template>
      </el-table-column>
      <el-table-column label="时间" width="150">
        <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
      </el-table-column>
    </el-table>
    <template #footer>
      <el-button @click="jobsVisible = false">关闭</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { ref, reactive, onMounted, onUnmounted } from 'vue'
import { Plus, Edit, Delete, Refresh } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  getAgents, createAgent, updateAgent, deleteAgent, regenAgentToken, getAgentJobs,
} from '../../api/agents'

const loading = ref(false)
const submitting = ref(false)
const tableData = ref([])

const dialogVisible = ref(false)
const editRow = ref(null)
const formRef = ref()
const form = reactive({ name: '', remark: '' })
const rules = { name: [{ required: true, message: '请输入名称' }] }

const tokenVisible = ref(false)
const oneTimeToken = ref('')

const jobsVisible = ref(false)
const jobsLoading = ref(false)
const jobsAgent = ref(null)
const jobs = ref([])

const formatTime = (t) => (t ? t.slice(0, 19).replace('T', ' ') : '')
const jobStatusType = (s) => ({ done: 'success', running: 'warning', sent: 'warning', pending: 'info', failed: 'danger' }[s] || 'info')
const jobStatusLabel = (s) => ({ pending: '待下发', sent: '已下发', running: '执行中', done: '已完成', failed: '通道失败' }[s] || s)

async function fetchData() {
  loading.value = true
  try {
    const res = await getAgents()
    tableData.value = Array.isArray(res) ? res : res.data || []
  } finally {
    loading.value = false
  }
}

function openDialog(row = null) {
  editRow.value = row
  Object.assign(form, { name: row?.name || '', remark: row?.remark || '' })
  dialogVisible.value = true
  formRef.value?.clearValidate()
}

async function handleSubmit() {
  await formRef.value.validate()
  submitting.value = true
  try {
    if (editRow.value) {
      await updateAgent(editRow.value.id, { name: form.name, remark: form.remark })
      ElMessage.success('更新成功')
    } else {
      const res = await createAgent({ name: form.name, remark: form.remark })
      oneTimeToken.value = res.data?.token || ''
      tokenVisible.value = true
    }
    dialogVisible.value = false
    fetchData()
  } finally {
    submitting.value = false
  }
}

async function handleRegenToken(row) {
  await ElMessageBox.confirm(
    `确定重置「${row.name}」的令牌吗？旧令牌立即失效，agent 将断连，需更新其配置后重启。`,
    '警告', { type: 'warning' },
  )
  const res = await regenAgentToken(row.id)
  oneTimeToken.value = res.data?.token || ''
  tokenVisible.value = true
}

async function handleToggle(row) {
  if (row.enabled) {
    await ElMessageBox.confirm(
      `确定禁用「${row.name}」吗？agent 将无法连接，其通道主机的任务模块会直接失败。`,
      '警告', { type: 'warning' },
    )
  }
  await updateAgent(row.id, { enabled: !row.enabled })
  ElMessage.success(row.enabled ? '已禁用' : '已启用')
  fetchData()
}

async function handleDelete(row) {
  await ElMessageBox.confirm(
    `确定删除「${row.name}」吗？其通道主机将回退为直连；存在未完成任务单元时禁止删除。`,
    '警告', { type: 'warning', confirmButtonText: '删除', confirmButtonClass: 'el-button--danger' },
  )
  await deleteAgent(row.id)
  ElMessage.success('已删除')
  fetchData()
}

async function openJobs(row) {
  jobsAgent.value = row
  jobsVisible.value = true
  jobsLoading.value = true
  try {
    const res = await getAgentJobs(row.id)
    jobs.value = res.data || []
  } finally {
    jobsLoading.value = false
  }
}

async function copyToken() {
  try {
    await navigator.clipboard.writeText(oneTimeToken.value)
    ElMessage.success('已复制')
  } catch {
    ElMessage.warning('复制失败，请手动选择复制')
  }
}

// 状态页 30s 自动刷新（在线/心跳时效性）
let timer = null
onMounted(() => {
  fetchData()
  timer = setInterval(fetchData, 30000)
})
onUnmounted(() => clearInterval(timer))
</script>

<style scoped>
.toolbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; gap: 12px; }
.hint { color: #909399; font-size: 13px; }
.muted { color: #c0c4cc; }
.link { color: var(--el-color-primary); text-decoration: none; }
</style>
