<template>
  <el-card shadow="never">
    <!-- 工具栏 -->
    <div class="toolbar">
      <div class="toolbar-left">
        <el-input
          v-model="filter.keyword"
          placeholder="搜索域名"
          clearable
          style="width:220px"
          @keyup.enter="fetchData"
          @clear="fetchData"
        />
        <el-select v-model="filter.cloudAccount" placeholder="云账号" clearable style="width:180px" @change="fetchData">
          <el-option v-for="a in accountOptions" :key="a.id" :label="a.name" :value="a.id" />
        </el-select>
      </div>
      <div v-if="authStore.isStaff">
        <el-button :icon="Download" @click="openImport">导入域名</el-button>
        <el-button type="primary" :icon="Plus" @click="openDialog()">新增域名</el-button>
      </div>
    </div>

    <!-- 域名表格 -->
    <el-table v-loading="loading" :data="tableData" stripe>
      <el-table-column prop="name" label="域名" min-width="180">
        <template #default="{ row }">
          <el-link type="primary" :underline="false" @click="openRecords(row)">{{ row.name }}</el-link>
        </template>
      </el-table-column>
      <el-table-column prop="cloud_account_name" label="云账号" min-width="130" />
      <el-table-column prop="provider_name" label="云厂商" width="100">
        <template #default="{ row }">
          <el-tag size="small" type="info">{{ row.provider_name }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="record_count" label="解析记录" width="90" />
      <el-table-column label="状态" width="80">
        <template #default="{ row }">
          <el-tag size="small" :type="row.status === 'normal' ? 'success' : 'danger'">
            {{ row.status === 'normal' ? '正常' : '异常' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="最近同步" width="170">
        <template #default="{ row }">
          <span v-if="row.last_synced_at">{{ formatTime(row.last_synced_at) }}</span>
          <span v-else class="muted">未同步</span>
        </template>
      </el-table-column>
      <el-table-column prop="remark" label="备注" min-width="120" show-overflow-tooltip />
      <el-table-column label="操作" :width="authStore.isStaff ? 300 : 110" fixed="right">
        <template #default="{ row }">
          <el-button size="small" @click="openRecords(row)">解析记录</el-button>
          <template v-if="authStore.isStaff">
            <el-button size="small" :loading="syncingId === row.id" @click="handleSync(row)">同步</el-button>
            <el-button size="small" :icon="Edit" @click="openDialog(row)">编辑</el-button>
            <el-button size="small" type="danger" :icon="Delete" @click="handleDelete(row)">删除</el-button>
          </template>
        </template>
      </el-table-column>
    </el-table>

    <!-- 分页 -->
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

  <!-- 新增/编辑域名弹窗 -->
  <el-dialog v-model="dialogVisible" :title="editRow ? '编辑域名' : '新增域名'" width="480px">
    <el-form ref="formRef" :model="form" :rules="rules" label-width="90px">
      <el-form-item label="域名" prop="name">
        <el-input v-model="form.name" placeholder="如 example.com" :disabled="!!editRow" />
      </el-form-item>
      <el-form-item label="云账号" prop="cloud_account">
        <el-select v-model="form.cloud_account" style="width:100%">
          <el-option v-for="a in accountOptions" :key="a.id" :label="`${a.name}（${a.provider_name}）`" :value="a.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="备注">
        <el-input v-model="form.remark" type="textarea" :rows="2" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="dialogVisible = false">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="handleSubmit">确定</el-button>
    </template>
  </el-dialog>

  <!-- 导入域名弹窗 -->
  <el-dialog v-model="importVisible" title="从云账号导入托管域名" width="440px">
    <el-form label-width="90px">
      <el-form-item label="云账号">
        <el-select v-model="importAccount" style="width:100%">
          <el-option v-for="a in accountOptions" :key="a.id" :label="`${a.name}（${a.provider_name}）`" :value="a.id" />
        </el-select>
      </el-form-item>
    </el-form>
    <div class="tip">拉取该账号在厂商 DNS 托管的全部域名，已存在的自动跳过。</div>
    <template #footer>
      <el-button @click="importVisible = false">取消</el-button>
      <el-button type="primary" :loading="importing" :disabled="!importAccount" @click="handleImport">导入</el-button>
    </template>
  </el-dialog>

  <!-- 解析记录抽屉 -->
  <el-drawer v-model="recordsVisible" :title="`解析记录 — ${currentDomain?.name || ''}`" size="72%">
    <div class="toolbar">
      <div class="toolbar-left">
        <el-input
          v-model="recordFilter.keyword"
          placeholder="搜索主机记录"
          clearable
          style="width:180px"
          @keyup.enter="fetchRecords"
          @clear="fetchRecords"
        />
        <el-select v-model="recordFilter.type" placeholder="类型" clearable style="width:110px" @change="fetchRecords">
          <el-option v-for="t in RECORD_TYPES" :key="t" :label="t" :value="t" />
        </el-select>
        <el-select v-model="recordFilter.syncStatus" placeholder="同步状态" clearable style="width:120px" @change="fetchRecords">
          <el-option label="已同步" value="synced" />
          <el-option label="待推送" value="pending" />
          <el-option label="推送失败" value="failed" />
        </el-select>
      </div>
      <el-button v-if="authStore.isStaff" type="primary" :icon="Plus" @click="openRecordDialog()">新增记录</el-button>
    </div>

    <el-table v-loading="recordsLoading" :data="recordRows" stripe>
      <el-table-column prop="rr" label="主机记录" min-width="120" />
      <el-table-column prop="type" label="类型" width="80">
        <template #default="{ row }">
          <el-tag size="small" type="info">{{ row.type }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="value" label="记录值" min-width="180" show-overflow-tooltip />
      <el-table-column prop="ttl" label="TTL" width="70" />
      <el-table-column label="绑定" min-width="120">
        <template #default="{ row }">
          <template v-if="row.target_type === 'server'">
            <el-tag size="small" type="success">服务器</el-tag>
            <span style="margin-left:4px">{{ row.server_name }}</span>
          </template>
          <el-tag v-else-if="row.target_type !== 'none'" size="small" type="warning">
            {{ TARGET_LABELS[row.target_type] }}
          </el-tag>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column label="启用" width="70">
        <template #default="{ row }">
          <el-tag size="small" :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '启用' : '暂停' }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="同步" width="110">
        <template #default="{ row }">
          <el-tooltip v-if="row.sync_status === 'failed'" :content="row.sync_error" placement="top">
            <el-tag size="small" type="danger">推送失败</el-tag>
          </el-tooltip>
          <el-tag v-else-if="row.sync_status === 'pending'" size="small" type="warning">待推送</el-tag>
          <el-tag v-else size="small" type="success">已同步</el-tag>
        </template>
      </el-table-column>
      <el-table-column v-if="authStore.isStaff" label="操作" width="220" fixed="right">
        <template #default="{ row }">
          <el-button
            v-if="row.sync_status !== 'synced'"
            size="small"
            type="warning"
            :loading="retryingId === row.id"
            @click="handleRetry(row)"
          >重推</el-button>
          <el-button size="small" :icon="Edit" @click="openRecordDialog(row)">编辑</el-button>
          <el-button size="small" type="danger" :icon="Delete" @click="handleRecordDelete(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-pagination
      v-model:current-page="recordPage"
      v-model:page-size="recordPageSize"
      :total="recordTotal"
      :page-sizes="[20, 50, 100]"
      layout="total, sizes, prev, pager, next"
      style="margin-top: 16px; justify-content: flex-end"
      @change="fetchRecords"
    />
  </el-drawer>

  <!-- 新增/编辑解析记录弹窗 -->
  <el-dialog v-model="recordDialogVisible" :title="editRecord ? '编辑解析记录' : '新增解析记录'" width="540px">
    <el-form ref="recordFormRef" :model="recordForm" :rules="recordRules" label-width="90px">
      <el-form-item label="主机记录" prop="rr">
        <el-input v-model="recordForm.rr" placeholder="如 www、@、*">
          <template #append>.{{ currentDomain?.name }}</template>
        </el-input>
      </el-form-item>
      <el-form-item label="类型" prop="type">
        <el-select v-model="recordForm.type" style="width:100%">
          <el-option v-for="t in RECORD_TYPES" :key="t" :label="t" :value="t" />
        </el-select>
      </el-form-item>
      <el-form-item label="绑定类型">
        <el-select v-model="recordForm.target_type" style="width:100%">
          <el-option v-for="(label, key) in TARGET_LABELS" :key="key" :label="label" :value="key" />
        </el-select>
        <div v-if="['lb', 'ga', 'cdn'].includes(recordForm.target_type)" class="tip">
          LB / GA / CDN 资源模块暂未接入，记录值请手工填写目标地址
        </div>
      </el-form-item>
      <el-form-item v-if="recordForm.target_type === 'server'" label="服务器" prop="server">
        <el-select v-model="recordForm.server" filterable style="width:100%">
          <el-option
            v-for="s in serverOptions"
            :key="s.id"
            :label="`${s.name}（${s.public_ip || s.private_ip}）`"
            :value="s.id"
          />
        </el-select>
        <div class="tip">记录值自动取主机 IP（公网优先）</div>
      </el-form-item>
      <el-form-item v-else label="记录值" prop="value">
        <el-input v-model="recordForm.value" :placeholder="recordForm.type === 'CNAME' ? '目标域名' : 'IP 或记录内容'" />
      </el-form-item>
      <el-form-item label="TTL(秒)">
        <el-input-number v-model="recordForm.ttl" :min="1" :max="86400" :step="60" style="width:100%" />
      </el-form-item>
      <el-form-item label="解析线路">
        <el-select v-model="recordForm.line" style="width:100%">
          <el-option v-for="(label, key) in LINE_LABELS" :key="key" :label="label" :value="key" />
        </el-select>
      </el-form-item>
      <el-form-item label="启用">
        <el-switch v-model="recordForm.enabled" />
      </el-form-item>
      <el-form-item label="备注">
        <el-input v-model="recordForm.remark" type="textarea" :rows="2" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="recordDialogVisible = false">取消</el-button>
      <el-button type="primary" :loading="recordSubmitting" @click="handleRecordSubmit">确定</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { Plus, Edit, Delete, Download } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useAuthStore } from '../../stores/auth'
import { getServers } from '../../api/assets'
import {
  getCloudAccounts, getDomains, createDomain, updateDomain, deleteDomain,
  importDomains, syncDomainRecords,
  getDnsRecords, createDnsRecord, updateDnsRecord, deleteDnsRecord, retrySyncDnsRecord,
} from '../../api/cloud'

const authStore = useAuthStore()

const RECORD_TYPES = ['A', 'AAAA', 'CNAME', 'TXT', 'MX', 'NS', 'SRV', 'CAA']
const TARGET_LABELS = { none: '无绑定', server: '服务器', lb: '负载均衡', ga: '全球加速', cdn: 'CDN', custom: '自定义' }
const LINE_LABELS = { default: '默认', telecom: '电信', unicom: '联通', mobile: '移动', oversea: '境外' }

// ---------- 域名列表 ----------
const loading = ref(false)
const submitting = ref(false)
const syncingId = ref(null)
const tableData = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const filter = reactive({ keyword: '', cloudAccount: '' })
const accountOptions = ref([])
const dialogVisible = ref(false)
const editRow = ref(null)
const formRef = ref()

const form = reactive({ name: '', cloud_account: null, remark: '' })
const rules = {
  name: [{ required: true, message: '请输入域名' }],
  cloud_account: [{ required: true, message: '请选择云账号' }],
}

function formatTime(t) {
  return t ? t.replace('T', ' ').slice(0, 19) : ''
}

async function fetchData() {
  loading.value = true
  try {
    const params = { page: page.value, page_size: pageSize.value }
    if (filter.keyword) params.keyword = filter.keyword
    if (filter.cloudAccount) params.cloud_account = filter.cloudAccount
    const res = await getDomains(params)
    tableData.value = res.data?.results || []
    total.value = res.data?.total || 0
  } finally {
    loading.value = false
  }
}

function openDialog(row = null) {
  editRow.value = row
  Object.assign(form, row ? { ...row } : { name: '', cloud_account: null, remark: '' })
  dialogVisible.value = true
  formRef.value?.clearValidate()
}

async function handleSubmit() {
  await formRef.value.validate()
  submitting.value = true
  try {
    if (editRow.value) {
      await updateDomain(editRow.value.id, form)
      ElMessage.success('更新成功')
    } else {
      await createDomain(form)
      ElMessage.success('新增成功')
    }
    dialogVisible.value = false
    fetchData()
  } finally {
    submitting.value = false
  }
}

async function handleDelete(row) {
  await ElMessageBox.confirm(
    `确定删除域名「${row.name}」吗？仅删除本地管理数据（含 ${row.record_count} 条本地解析记录），厂商侧解析不受影响。`,
    '警告',
    { type: 'warning' },
  )
  await deleteDomain(row.id)
  ElMessage.success('删除成功')
  fetchData()
}

// 从厂商侧全量同步该域名的解析记录到本地
async function handleSync(row) {
  syncingId.value = row.id
  try {
    const res = await syncDomainRecords(row.id)
    ElMessage.success(res.msg || '同步完成')
    fetchData()
    if (recordsVisible.value && currentDomain.value?.id === row.id) fetchRecords()
  } finally {
    syncingId.value = null
  }
}

// ---------- 导入域名 ----------
const importVisible = ref(false)
const importing = ref(false)
const importAccount = ref(null)

function openImport() {
  importAccount.value = null
  importVisible.value = true
}

async function handleImport() {
  importing.value = true
  try {
    const res = await importDomains({ cloud_account: importAccount.value })
    ElMessage.success(res.msg || '导入完成')
    importVisible.value = false
    fetchData()
  } finally {
    importing.value = false
  }
}

// ---------- 解析记录 ----------
const recordsVisible = ref(false)
const recordsLoading = ref(false)
const currentDomain = ref(null)
const recordRows = ref([])
const recordTotal = ref(0)
const recordPage = ref(1)
const recordPageSize = ref(20)
const recordFilter = reactive({ keyword: '', type: '', syncStatus: '' })
const retryingId = ref(null)

const recordDialogVisible = ref(false)
const recordSubmitting = ref(false)
const editRecord = ref(null)
const recordFormRef = ref()
const serverOptions = ref([])

const emptyRecordForm = () => ({
  rr: '', type: 'A', value: '', ttl: 600, line: 'default',
  target_type: 'none', server: null, enabled: true, remark: '',
})
const recordForm = reactive(emptyRecordForm())
const recordRules = {
  rr: [{ required: true, message: '请输入主机记录' }],
  type: [{ required: true, message: '请选择类型' }],
  server: [{ required: true, message: '请选择服务器' }],
  value: [{ required: true, message: '请输入记录值' }],
}

function openRecords(row) {
  currentDomain.value = row
  recordPage.value = 1
  recordFilter.keyword = ''
  recordFilter.type = ''
  recordFilter.syncStatus = ''
  recordsVisible.value = true
  fetchRecords()
}

async function fetchRecords() {
  recordsLoading.value = true
  try {
    const params = {
      domain: currentDomain.value.id,
      page: recordPage.value,
      page_size: recordPageSize.value,
    }
    if (recordFilter.keyword) params.keyword = recordFilter.keyword
    if (recordFilter.type) params.type = recordFilter.type
    if (recordFilter.syncStatus) params.sync_status = recordFilter.syncStatus
    const res = await getDnsRecords(params)
    recordRows.value = res.data?.results || []
    recordTotal.value = res.data?.total || 0
  } finally {
    recordsLoading.value = false
  }
}

function openRecordDialog(row = null) {
  editRecord.value = row
  Object.assign(recordForm, row ? { ...row } : emptyRecordForm())
  recordDialogVisible.value = true
  recordFormRef.value?.clearValidate()
}

async function handleRecordSubmit() {
  await recordFormRef.value.validate()
  recordSubmitting.value = true
  try {
    const payload = { ...recordForm, domain: currentDomain.value.id }
    if (editRecord.value) {
      await updateDnsRecord(editRecord.value.id, payload)
      ElMessage.success('更新成功')
    } else {
      await createDnsRecord(payload)
      ElMessage.success('新增成功')
    }
    recordDialogVisible.value = false
    fetchRecords()
  } finally {
    recordSubmitting.value = false
  }
}

async function handleRecordDelete(row) {
  await ElMessageBox.confirm(
    `确定删除解析记录「${row.rr}.${currentDomain.value.name} ${row.type}」吗？将同步删除厂商侧解析，立即生效。`,
    '警告',
    { type: 'warning' },
  )
  await deleteDnsRecord(row.id)
  ElMessage.success('删除成功')
  fetchRecords()
}

async function handleRetry(row) {
  retryingId.value = row.id
  try {
    const res = await retrySyncDnsRecord(row.id)
    ElMessage.success(res.msg || '重推成功')
    fetchRecords()
  } finally {
    retryingId.value = null
  }
}

onMounted(async () => {
  fetchData()
  // 云账号下拉（域名归属/导入用）；服务器下拉（记录绑定用，page_size=0 全量）
  if (authStore.isStaff) {
    const [accRes, svrRes] = await Promise.all([
      getCloudAccounts({ page_size: 0 }),
      getServers({ page_size: 0 }),
    ])
    accountOptions.value = accRes.data?.results || []
    serverOptions.value = svrRes.data?.results || []
  }
})
</script>

<style scoped>
.toolbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.toolbar-left { display: flex; gap: 8px; }
.muted { color: #909399; }
.tip { font-size: 12px; color: #909399; line-height: 1.6; }
</style>
