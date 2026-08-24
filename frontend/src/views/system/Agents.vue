<template>
  <el-card shadow="never">
    <el-tabs v-model="activeTab">
      <el-tab-pane label="Agent 节点" name="nodes">
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
      </el-tab-pane>

      <!-- 通道路由：主机 channel_mode=auto 时按此规则解析执行通道 -->
      <el-tab-pane label="通道路由" name="routes">
        <div class="toolbar">
          <span class="hint">
            按「项目组 + 环境」把主机默认路由到某个 Agent，只对执行通道为「自动」的主机生效；
            环境留空 = 该项目组的默认规则，被同项目组的精确环境规则覆盖。
            主机侧选「强制直连」或「指定 Agent」时不受规则影响。
          </span>
          <div>
            <el-button :icon="Refresh" @click="fetchRoutes">刷新</el-button>
            <el-button type="primary" :icon="Plus" @click="openRouteDialog()">新增规则</el-button>
          </div>
        </div>
        <el-table v-loading="routesLoading" :data="routes" stripe>
          <el-table-column prop="project_group_name" label="项目组" min-width="130" />
          <el-table-column label="环境" width="120">
            <template #default="{ row }">
              <span v-if="row.environment_name">{{ row.environment_name }}</span>
              <span v-else class="muted">不限（项目组默认）</span>
            </template>
          </el-table-column>
          <el-table-column label="执行通道" min-width="130">
            <template #default="{ row }">
              <el-tag size="small" type="warning">{{ row.agent_name }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag size="small" :type="row.enabled ? 'success' : 'info'">
                {{ row.enabled ? '生效中' : '已停用' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="remark" label="备注" min-width="140" show-overflow-tooltip>
            <template #default="{ row }">{{ row.remark || '—' }}</template>
          </el-table-column>
          <el-table-column label="操作" width="220" fixed="right">
            <template #default="{ row }">
              <el-button size="small" :icon="Edit" @click="openRouteDialog(row)">编辑</el-button>
              <el-button size="small" :type="row.enabled ? 'info' : 'success'" @click="toggleRoute(row)">
                {{ row.enabled ? '停用' : '启用' }}
              </el-button>
              <el-button size="small" type="danger" :icon="Delete" @click="deleteRoute(row)" />
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>
    </el-tabs>
  </el-card>

  <!-- 新增 / 编辑路由规则 -->
  <el-dialog v-model="routeDialogVisible" :title="editRoute ? '编辑通道路由' : '新增通道路由'" width="520px">
    <el-form ref="routeFormRef" :model="routeForm" :rules="routeRules" label-width="90px">
      <el-form-item label="项目组" prop="project_group">
        <el-select v-model="routeForm.project_group" placeholder="必选" style="width:100%" @change="loadPreview">
          <el-option v-for="o in projectGroups" :key="o.id" :label="o.name" :value="o.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="环境">
        <el-select
          v-model="routeForm.environment" clearable placeholder="不限（该项目组默认规则）"
          style="width:100%" @change="loadPreview"
        >
          <el-option v-for="o in environments" :key="o.id" :label="o.name" :value="o.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="执行 Agent" prop="agent">
        <el-select v-model="routeForm.agent" placeholder="必选" style="width:100%">
          <el-option v-for="o in tableData" :key="o.id" :label="o.name" :value="o.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="启用">
        <el-switch v-model="routeForm.enabled" />
      </el-form-item>
      <el-form-item label="备注">
        <el-input v-model="routeForm.remark" placeholder="如所属 NAT 网络" />
      </el-form-item>
      <el-alert v-if="preview" :type="preview.count ? 'warning' : 'info'" :closable="false" show-icon>
        该规则将接管 {{ preview.count }} 台主机的执行通道{{ preview.count ? '：' + preview.names : '（当前无「自动」通道的主机）' }}
      </el-alert>
    </el-form>
    <template #footer>
      <el-button @click="routeDialogVisible = false">取消</el-button>
      <el-button type="primary" :loading="routeSubmitting" @click="submitRoute">确定</el-button>
    </template>
  </el-dialog>

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
  getAgentRoutes, createAgentRoute, updateAgentRoute, deleteAgentRoute, previewAgentRoute,
} from '../../api/agents'
import { projectGroupApi, environmentApi, fetchDictOptions } from '../../api/assets'

const activeTab = ref('nodes')

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

// ── 通道路由 ──────────────────────────────────────────
const routes = ref([])
const routesLoading = ref(false)
const routeDialogVisible = ref(false)
const routeSubmitting = ref(false)
const editRoute = ref(null)
const routeFormRef = ref()
const routeForm = reactive(
  { project_group: null, environment: null, agent: null, enabled: true, remark: '' })
const routeRules = {
  project_group: [{ required: true, message: '请选择项目组' }],
  agent: [{ required: true, message: '请选择 Agent' }],
}
const projectGroups = ref([])
const environments = ref([])
const preview = ref(null)

async function fetchRoutes() {
  routesLoading.value = true
  try {
    const res = await getAgentRoutes()
    routes.value = Array.isArray(res) ? res : res.data || []
  } finally {
    routesLoading.value = false
  }
}

async function loadDicts() {
  const [groups, envs] = await Promise.all([
    fetchDictOptions(projectGroupApi), fetchDictOptions(environmentApi),
  ])
  projectGroups.value = groups.data?.results || []
  environments.value = envs.data?.results || []
}

// 影响面预览：建规则前先看会接管哪些主机（只统计通道为「自动」的）
async function loadPreview() {
  preview.value = null
  if (!routeForm.project_group) return
  const res = await previewAgentRoute({
    project_group: routeForm.project_group, environment: routeForm.environment || undefined,
  })
  const data = res.data || {}
  const hosts = data.hosts || []
  preview.value = {
    count: data.count || 0,
    names: hosts.slice(0, 5).map((h) => h.name).join('、') + (hosts.length > 5 ? ' 等' : ''),
  }
}

function openRouteDialog(row = null) {
  editRoute.value = row
  Object.assign(routeForm, {
    project_group: row?.project_group || null,
    environment: row?.environment || null,
    agent: row?.agent || null,
    enabled: row ? row.enabled : true,
    remark: row?.remark || '',
  })
  preview.value = null
  routeDialogVisible.value = true
  routeFormRef.value?.clearValidate()
  if (row) loadPreview()
}

async function submitRoute() {
  await routeFormRef.value.validate()
  routeSubmitting.value = true
  try {
    const payload = { ...routeForm, environment: routeForm.environment || null }
    if (editRoute.value) {
      await updateAgentRoute(editRoute.value.id, payload)
      ElMessage.success('更新成功')
    } else {
      await createAgentRoute(payload)
      ElMessage.success('新增成功')
    }
    routeDialogVisible.value = false
    fetchRoutes()
  } finally {
    routeSubmitting.value = false
  }
}

async function toggleRoute(row) {
  if (row.enabled) {
    await ElMessageBox.confirm(
      `确定停用该规则吗？命中该规则的「自动」通道主机将回退为直连——若它们在 NAT 内网，`
      + `直连不可达，任务会以 SSH 超时失败。`,
      '警告', { type: 'warning' },
    )
  }
  await updateAgentRoute(row.id, { enabled: !row.enabled })
  ElMessage.success(row.enabled ? '已停用' : '已启用')
  fetchRoutes()
}

async function deleteRoute(row) {
  await ElMessageBox.confirm(
    `确定删除「${row.project_group_name} / ${row.environment_name || '不限环境'}」的路由规则吗？`,
    '警告', { type: 'warning', confirmButtonText: '删除', confirmButtonClass: 'el-button--danger' },
  )
  await deleteAgentRoute(row.id)
  ElMessage.success('已删除')
  fetchRoutes()
}

// 状态页 30s 自动刷新（在线/心跳时效性）
let timer = null
onMounted(() => {
  fetchData()
  fetchRoutes()
  loadDicts()
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
