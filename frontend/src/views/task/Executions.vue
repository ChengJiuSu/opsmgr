<template>
  <el-card shadow="never">
    <!-- 工具栏 -->
    <div class="toolbar">
      <div class="toolbar-left">
        <el-input v-model="filter.keyword" placeholder="任务名称" clearable style="width:160px" @keyup.enter="handleSearch" @clear="handleSearch" />
        <el-select v-model="filter.task_type" placeholder="类型" clearable style="width:120px" @change="handleSearch">
          <el-option label="命令" value="command" />
          <el-option label="Playbook" value="playbook" />
          <el-option label="平台方法" value="method" />
        </el-select>
        <el-select v-model="filter.status" placeholder="状态" clearable style="width:110px" @change="handleSearch">
          <el-option label="等待中" value="pending" />
          <el-option label="执行中" value="running" />
          <el-option label="成功" value="success" />
          <el-option label="失败" value="failed" />
        </el-select>
        <el-button :icon="Search" @click="handleSearch">查询</el-button>
        <el-button :icon="Refresh" @click="fetchData">刷新</el-button>
      </div>
      <el-button v-if="authStore.isStaff" type="primary" :icon="Plus" @click="openDialog">新建任务</el-button>
    </div>

    <!-- 表格 -->
    <el-table v-loading="loading" :data="tableData" stripe>
      <el-table-column prop="name" label="任务名称" min-width="140" show-overflow-tooltip />
      <el-table-column label="类型" width="100">
        <template #default="{ row }">
          <el-tag size="small" :type="typeTagType(row.task_type)">
            {{ row.task_type_display }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="内容 / 目标" min-width="180" show-overflow-tooltip>
        <template #default="{ row }">
          <span v-if="row.task_type === 'method'">{{ row.method_name || '-' }}<span v-if="row.tags" class="muted"> · {{ row.tags }}</span></span>
          <span v-else-if="row.task_type === 'playbook'">{{ row.playbook || '-' }}<span v-if="row.tags" class="muted"> · {{ row.tags }}</span></span>
          <span v-else class="muted">{{ (row.command || '').slice(0, 40) || '-' }}</span>
          <span class="muted"> · {{ row.servers?.length || 0 }} 台</span>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tag size="small" :type="statusType(row.status)">
            <el-icon v-if="row.status === 'running'" class="is-loading"><Loading /></el-icon>
            {{ row.status_display }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="返回码" width="80">
        <template #default="{ row }">{{ row.return_code ?? '-' }}</template>
      </el-table-column>
      <el-table-column label="耗时" width="80">
        <template #default="{ row }">{{ row.duration != null ? row.duration + 's' : '-' }}</template>
      </el-table-column>
      <el-table-column prop="created_by_name" label="发起人" width="100" />
      <el-table-column prop="created_at" label="创建时间" width="160">
        <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="200" fixed="right">
        <template #default="{ row }">
          <el-button size="small" :icon="View" @click="openOutput(row)">输出</el-button>
          <el-button v-if="authStore.isStaff" size="small" :icon="RefreshRight" :disabled="row.status === 'running'" @click="handleRerun(row)">重跑</el-button>
          <el-button v-if="authStore.isStaff" size="small" type="danger" :icon="Delete" @click="handleDelete(row)" />
        </template>
      </el-table-column>
    </el-table>

    <el-pagination
      v-model:current-page="page"
      v-model:page-size="pageSize"
      :total="total"
      :page-sizes="[20, 50, 100]"
      layout="total, sizes, prev, pager, next"
      style="margin-top: 16px; justify-content: flex-end"
      @change="fetchData"
    />
  </el-card>

  <!-- 新建任务弹窗 -->
  <el-dialog v-model="dialogVisible" title="新建执行任务" width="640px" top="6vh">
    <el-form ref="formRef" :model="form" :rules="rules" label-width="90px">
      <el-form-item label="任务名称" prop="name">
        <el-input v-model="form.name" placeholder="如：重启 nginx" />
      </el-form-item>
      <el-form-item label="任务类型">
        <el-radio-group v-model="form.task_type">
          <el-radio-button value="command">命令</el-radio-button>
          <el-radio-button value="playbook">Playbook</el-radio-button>
          <el-radio-button value="method">平台方法</el-radio-button>
        </el-radio-group>
      </el-form-item>

      <!-- 命令模式 -->
      <el-form-item v-if="form.task_type === 'command'" label="Shell 命令" prop="command">
        <el-input v-model="form.command" type="textarea" :rows="3" placeholder="如：systemctl restart nginx" />
      </el-form-item>

      <!-- Playbook 模式 -->
      <el-form-item v-if="form.task_type === 'playbook'" label="剧本文件" prop="playbook">
        <el-select v-model="form.playbook" filterable placeholder="选择 Ansible 项目目录内的剧本" style="width:100%">
          <el-option v-for="p in playbookOptions" :key="p" :label="p" :value="p" />
        </el-select>
      </el-form-item>

      <!-- 平台方法模式 -->
      <template v-if="form.task_type === 'method'">
        <el-form-item label="平台方法" prop="method">
          <el-select v-model="form.method" filterable placeholder="选择已启用的方法" style="width:100%" @change="onMethodChange">
            <el-option v-for="m in methodOptions" :key="m.id" :label="`${m.name} (${m.provider_display})`" :value="m.id" />
          </el-select>
        </el-form-item>
        <el-alert v-if="selectedMethod" type="info" :closable="false" style="margin-bottom:12px">
          <template v-if="selectedMethod.provider === 'ansible_playbook'">
            剧本：<code>{{ selectedMethod.config?.playbook }}</code>
            <span v-if="selectedMethod.config?.tags"> · 默认 tags：<code>{{ selectedMethod.config.tags }}</code></span>
          </template>
          <template v-else>{{ selectedMethod.provider_display }}（预留 provider）</template>
        </el-alert>
      </template>

      <!-- 目标主机（三类任务共用）：按项目/渠道/环境筛选，仅显示已规划服务的主机 -->
      <el-form-item label="目标主机" prop="server_ids">
        <div class="dim-filter">
          <el-select v-model="dimFilter.project_group" placeholder="项目组" clearable filterable @change="loadServers">
            <el-option v-for="d in dictOptions.projects" :key="d.id" :label="d.name" :value="d.id" />
          </el-select>
          <el-select v-model="dimFilter.channel" placeholder="渠道" clearable filterable @change="loadServers">
            <el-option v-for="d in dictOptions.channels" :key="d.id" :label="d.name" :value="d.id" />
          </el-select>
          <el-select v-model="dimFilter.env" placeholder="环境" clearable filterable @change="loadServers">
            <el-option v-for="d in dictOptions.envs" :key="d.id" :label="d.name" :value="d.id" />
          </el-select>
        </div>
        <el-select
          v-model="form.server_ids" multiple filterable clearable
          :loading="serversLoading" placeholder="选择目标服务器" style="width:100%"
        >
          <el-option v-for="s in serverOptions" :key="s.id" :label="serverLabel(s)" :value="s.id" />
        </el-select>
        <div class="form-tip">
          仅显示已在「服务器分配」中规划服务的主机（未分配服务的主机不具备执行条件），可按项目组/渠道/环境缩小范围
        </div>
      </el-form-item>

      <el-row :gutter="16">
        <el-col :span="12">
          <el-form-item label="连接凭据">
            <el-select v-model="form.credential" clearable placeholder="默认免密/密钥" style="width:100%">
              <el-option v-for="c in credentialOptions" :key="c.id" :label="c.name" :value="c.id" />
            </el-select>
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item v-if="form.task_type === 'command'" label="主机匹配">
            <el-input v-model="form.pattern" placeholder="ansible 主机模式，默认 all" />
          </el-form-item>
          <el-form-item v-else label="Limit">
            <el-input v-model="form.limit" placeholder="进一步限定主机/分组，可留空" />
          </el-form-item>
        </el-col>
      </el-row>

      <!-- Ansible 参数（playbook / 方法任务） -->
      <template v-if="form.task_type !== 'command'">
        <el-form-item v-if="form.task_type === 'playbook' || selectedMethod?.provider === 'ansible_playbook'" label="Tags">
          <el-input
            v-model="form.tags"
            :placeholder="form.task_type === 'method' ? `留空用方法默认：${selectedMethod?.config?.tags || '（全部）'}` : '如 nginx,restart，留空执行全部'"
          />
        </el-form-item>
        <el-form-item label="额外变量">
          <el-input v-model="form.extra_vars" type="textarea" :rows="2" placeholder="--extra-vars，如 key=val 或 JSON" />
        </el-form-item>
      </template>

      <el-form-item label="超时(秒)">
        <el-input-number v-model="form.timeout" :min="10" :max="3600" style="width:100%" />
      </el-form-item>
    </el-form>
    <el-alert type="warning" :closable="false" show-icon style="margin-bottom: 4px">
      任务将通过 Ansible 在目标主机上执行，请谨慎操作。
    </el-alert>
    <template #footer>
      <el-button @click="dialogVisible = false">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="handleSubmit">提交执行</el-button>
    </template>
  </el-dialog>

  <!-- 输出抽屉 -->
  <el-drawer v-model="outputVisible" :title="`执行输出 - ${current?.name || ''}`" size="60%">
    <div class="output-meta">
      <el-tag :type="statusType(current?.status)" size="small">{{ current?.status_display || current?.status }}</el-tag>
      <span>返回码：{{ current?.return_code ?? '-' }}</span>
      <span v-if="current?.duration != null">耗时：{{ current.duration }}s</span>
      <el-tag v-if="polling" type="info" size="small">实时刷新中…</el-tag>
    </div>
    <pre class="output-box">{{ current?.output || '（暂无输出）' }}</pre>
  </el-drawer>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onUnmounted } from 'vue'
import { Plus, Delete, Search, Refresh, RefreshRight, View, Loading } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useAuthStore } from '../../stores/auth'
import {
  getServers, fetchDictOptions, projectGroupApi, channelApi, environmentApi,
} from '../../api/assets'
import {
  getTasks, createTask, deleteTask, rerunTask, getTaskOutput,
  getCredentials, getMethods, getPlaybooks,
} from '../../api/tasks'

const authStore = useAuthStore()
const loading = ref(false)
const submitting = ref(false)
const tableData = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const filter = reactive({ keyword: '', task_type: '', status: '' })

const serverOptions = ref([])
const serversLoading = ref(false)
const credentialOptions = ref([])
const methodOptions = ref([])
const playbookOptions = ref([])
// 目标主机维度筛选（项目组/渠道/环境）与字典下拉数据
const dimFilter = reactive({ project_group: '', channel: '', env: '' })
const dictOptions = reactive({ projects: [], channels: [], envs: [] })

// 后端 executable=true 已只返回已规划服务的主机，标签直接展示服务类型
function serverLabel(s) {
  const codes = (s.services || [])
    .filter(x => x.status === 'planned' || x.status === 'deployed')
    .map(x => x.service_type_name)
  return `${s.name} (${s.ip}) [${codes.join('/')}]`
}

const dialogVisible = ref(false)
const formRef = ref()
const emptyForm = () => ({
  name: '', task_type: 'command',
  command: '', server_ids: [], credential: null, pattern: 'all',
  playbook: '', method: null, tags: '', limit: '', extra_vars: '',
  timeout: 300,
})
const form = reactive(emptyForm())
const selectedMethod = computed(() => methodOptions.value.find((m) => m.id === form.method) || null)
const rules = {
  name: [{ required: true, message: '请输入任务名称' }],
  command: [{
    validator: (_r, v, cb) => (form.task_type !== 'command' || (v || '').trim()) ? cb() : cb(new Error('请输入命令')),
  }],
  playbook: [{
    validator: (_r, v, cb) => (form.task_type !== 'playbook' || v) ? cb() : cb(new Error('请选择剧本文件')),
  }],
  method: [{
    validator: (_r, v, cb) => (form.task_type !== 'method' || v) ? cb() : cb(new Error('请选择平台方法')),
  }],
  // 三类任务的执行主机统一来自平台台账，均必选
  server_ids: [{
    validator: (_r, v, cb) => (v && v.length) ? cb() : cb(new Error('请至少选择一台主机')),
  }],
}

const outputVisible = ref(false)
const current = ref(null)
const polling = ref(false)
let pollTimer = null
let listTimer = null

const statusType = (s) => ({ pending: 'info', running: 'warning', success: 'success', failed: 'danger' }[s] || 'info')
const typeTagType = (t) => ({ command: 'info', playbook: 'success', method: 'warning' }[t] || 'info')
const formatTime = (t) => (t ? t.slice(0, 19).replace('T', ' ') : '')

async function fetchData() {
  loading.value = true
  try {
    const params = { page: page.value, page_size: pageSize.value }
    if (filter.keyword) params.keyword = filter.keyword
    if (filter.task_type) params.task_type = filter.task_type
    if (filter.status) params.status = filter.status
    const res = await getTasks(params)
    tableData.value = res.data?.results || []
    total.value = res.data?.total || 0
  } finally {
    loading.value = false
  }
}

function handleSearch() {
  page.value = 1
  fetchData()
}

function onMethodChange() {
  form.tags = ''
  form.limit = ''
}

// 按维度筛选加载可执行主机（executable=true：仅已规划/已部署服务的主机）
async function loadServers() {
  serversLoading.value = true
  try {
    const params = { executable: 'true', page_size: 0 }
    if (dimFilter.project_group) params.project_group = dimFilter.project_group
    if (dimFilter.channel) params.channel = dimFilter.channel
    if (dimFilter.env) params.env = dimFilter.env
    const res = await getServers(params)
    serverOptions.value = res.data?.results || []
    // 维度切换后剔除已不在可选范围内的主机
    const available = new Set(serverOptions.value.map((s) => s.id))
    form.server_ids = form.server_ids.filter((id) => available.has(id))
  } finally {
    serversLoading.value = false
  }
}

async function loadDictOptions() {
  if (dictOptions.projects.length) return // 字典变化低频，弹窗生命周期内缓存
  const [proj, chan, env] = await Promise.allSettled([
    fetchDictOptions(projectGroupApi), fetchDictOptions(channelApi), fetchDictOptions(environmentApi),
  ])
  if (proj.status === 'fulfilled') dictOptions.projects = proj.value.data?.results || []
  if (chan.status === 'fulfilled') dictOptions.channels = chan.value.data?.results || []
  if (env.status === 'fulfilled') dictOptions.envs = env.value.data?.results || []
}

async function openDialog() {
  Object.assign(form, emptyForm())
  Object.assign(dimFilter, { project_group: '', channel: '', env: '' })
  dialogVisible.value = true
  formRef.value?.clearValidate()
  loadDictOptions()
  loadServers()
  const [cred, methods, playbooks] = await Promise.allSettled([
    getCredentials({ page_size: 200 }),
    getMethods({ page_size: 200, enabled: 'true' }),
    getPlaybooks(),
  ])
  if (cred.status === 'fulfilled') credentialOptions.value = cred.value.data?.results || []
  if (methods.status === 'fulfilled') methodOptions.value = methods.value.data?.results || []
  if (playbooks.status === 'fulfilled') playbookOptions.value = playbooks.value.data?.results || []
}

function buildPayload() {
  const p = {
    name: form.name, task_type: form.task_type, timeout: form.timeout,
    server_ids: form.server_ids, credential: form.credential,
  }
  if (form.task_type === 'command') {
    Object.assign(p, { command: form.command, pattern: form.pattern })
  } else if (form.task_type === 'playbook') {
    Object.assign(p, { playbook: form.playbook, tags: form.tags, limit: form.limit, extra_vars: form.extra_vars })
  } else {
    Object.assign(p, { method: form.method, tags: form.tags, limit: form.limit, extra_vars: form.extra_vars })
  }
  return p
}

async function handleSubmit() {
  await formRef.value.validate()
  submitting.value = true
  try {
    await createTask(buildPayload())
    ElMessage.success('任务已提交执行')
    dialogVisible.value = false
    fetchData()
  } finally {
    submitting.value = false
  }
}

async function handleRerun(row) {
  await rerunTask(row.id)
  ElMessage.success('已重新调度')
  fetchData()
}

async function handleDelete(row) {
  await ElMessageBox.confirm(`确定删除任务「${row.name}」吗？`, '警告', { type: 'warning' })
  await deleteTask(row.id)
  ElMessage.success('删除成功')
  fetchData()
}

function openOutput(row) {
  current.value = { ...row }
  outputVisible.value = true
  if (row.status === 'pending' || row.status === 'running') {
    startPolling(row.id)
  }
}

function startPolling(id) {
  stopPolling()
  polling.value = true
  pollTimer = setInterval(async () => {
    try {
      const res = await getTaskOutput(id)
      current.value = { ...current.value, ...res.data }
      if (res.data.status === 'success' || res.data.status === 'failed') {
        stopPolling()
        fetchData()
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

onMounted(() => {
  fetchData()
  listTimer = setInterval(() => {
    if (tableData.value.some((t) => t.status === 'running' || t.status === 'pending')) {
      fetchData()
    }
  }, 5000)
})

onUnmounted(() => {
  stopPolling()
  if (listTimer) clearInterval(listTimer)
})
</script>

<style scoped>
.toolbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.toolbar-left { display: flex; gap: 8px; flex-wrap: wrap; }
.muted { color: #909399; }
.form-tip { color: #909399; font-size: 12px; line-height: 1.5; margin-top: 4px; }
.dim-filter { display: flex; gap: 8px; width: 100%; margin-bottom: 8px; }
.dim-filter .el-select { flex: 1; }
code { background: #f5f7fa; padding: 1px 6px; border-radius: 3px; font-size: 12px; }
.output-meta { display: flex; gap: 16px; align-items: center; margin-bottom: 12px; color: #606266; font-size: 13px; }
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
  max-height: calc(100vh - 160px);
  overflow-y: auto;
}
</style>
