<template>
  <el-card shadow="never">
    <!-- 工具栏 -->
    <div class="toolbar">
      <div class="toolbar-left">
        <el-input v-model="filter.keyword" placeholder="任务名称" clearable style="width:160px" @keyup.enter="handleSearch" @clear="handleSearch" />
        <el-select v-model="filter.task_type" placeholder="类型" clearable style="width:120px" @change="handleSearch">
          <el-option label="命令" value="command" />
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
      <el-table-column label="类型" width="90">
        <template #default="{ row }">
          <el-tag size="small" :type="row.task_type === 'method' ? 'warning' : 'info'">
            {{ row.task_type_display }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="目标 / 方法" min-width="140" show-overflow-tooltip>
        <template #default="{ row }">
          <span v-if="row.task_type === 'method'">{{ row.method_name || '-' }}<span v-if="row.tags" class="muted"> · {{ row.tags }}</span></span>
          <span v-else>{{ row.servers?.length || 0 }} 台主机</span>
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
          <el-radio-button value="method">平台方法</el-radio-button>
        </el-radio-group>
      </el-form-item>

      <!-- 命令模式 -->
      <template v-if="form.task_type === 'command'">
        <el-form-item label="Shell 命令" prop="command">
          <el-input v-model="form.command" type="textarea" :rows="3" placeholder="如：systemctl restart nginx" />
        </el-form-item>
        <el-form-item label="目标主机" prop="server_ids">
          <el-select v-model="form.server_ids" multiple filterable placeholder="选择目标服务器" style="width:100%">
            <el-option v-for="s in serverOptions" :key="s.id" :label="`${s.name} (${s.ip})`" :value="s.id" />
          </el-select>
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
            <el-form-item label="主机匹配">
              <el-input v-model="form.pattern" placeholder="ansible 主机模式，默认 all" />
            </el-form-item>
          </el-col>
        </el-row>
      </template>

      <!-- 平台方法模式 -->
      <template v-else>
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
        <template v-if="selectedMethod?.provider === 'ansible_playbook'">
          <el-form-item label="Tags 覆盖">
            <el-input v-model="form.tags" :placeholder="`留空用方法默认：${selectedMethod?.config?.tags || '（全部）'}`" />
          </el-form-item>
          <el-row :gutter="16">
            <el-col :span="12">
              <el-form-item label="Limit">
                <el-input v-model="form.limit" placeholder="限定主机，可留空" />
              </el-form-item>
            </el-col>
            <el-col :span="12">
              <el-form-item label="Inventory">
                <el-input v-model="form.inventory" placeholder="覆盖，可留空" />
              </el-form-item>
            </el-col>
          </el-row>
          <el-form-item label="额外变量">
            <el-input v-model="form.extra_vars" type="textarea" :rows="2" placeholder="--extra-vars，如 key=val 或 JSON" />
          </el-form-item>
        </template>
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
import { getServers } from '../../api/assets'
import {
  getTasks, createTask, deleteTask, rerunTask, getTaskOutput, getCredentials, getMethods,
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
const credentialOptions = ref([])
const methodOptions = ref([])

const dialogVisible = ref(false)
const formRef = ref()
const emptyForm = () => ({
  name: '', task_type: 'command',
  command: '', server_ids: [], credential: null, pattern: 'all',
  method: null, tags: '', limit: '', inventory: '', extra_vars: '',
  timeout: 300,
})
const form = reactive(emptyForm())
const selectedMethod = computed(() => methodOptions.value.find((m) => m.id === form.method) || null)
const rules = {
  name: [{ required: true, message: '请输入任务名称' }],
  command: [{
    validator: (_r, v, cb) => (form.task_type !== 'command' || (v || '').trim()) ? cb() : cb(new Error('请输入命令')),
  }],
  server_ids: [{
    validator: (_r, v, cb) => (form.task_type !== 'command' || (v && v.length)) ? cb() : cb(new Error('请至少选择一台主机')),
  }],
  method: [{
    validator: (_r, v, cb) => (form.task_type !== 'method' || v) ? cb() : cb(new Error('请选择平台方法')),
  }],
}

const outputVisible = ref(false)
const current = ref(null)
const polling = ref(false)
let pollTimer = null
let listTimer = null

const statusType = (s) => ({ pending: 'info', running: 'warning', success: 'success', failed: 'danger' }[s] || 'info')
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
  form.inventory = ''
}

async function openDialog() {
  Object.assign(form, emptyForm())
  dialogVisible.value = true
  formRef.value?.clearValidate()
  const [srv, cred, methods] = await Promise.allSettled([
    getServers({ page_size: 200 }),
    getCredentials({ page_size: 200 }),
    getMethods({ page_size: 200, enabled: 'true' }),
  ])
  if (srv.status === 'fulfilled') serverOptions.value = srv.value.data?.results || []
  if (cred.status === 'fulfilled') credentialOptions.value = cred.value.data?.results || []
  if (methods.status === 'fulfilled') methodOptions.value = methods.value.data?.results || []
}

function buildPayload() {
  const p = { name: form.name, task_type: form.task_type, timeout: form.timeout }
  if (form.task_type === 'command') {
    Object.assign(p, {
      command: form.command, server_ids: form.server_ids,
      credential: form.credential, pattern: form.pattern,
    })
  } else {
    Object.assign(p, {
      method: form.method, tags: form.tags, limit: form.limit,
      inventory: form.inventory, extra_vars: form.extra_vars,
    })
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
