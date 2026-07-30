<template>
  <el-card shadow="never">
    <!-- 工具栏 -->
    <div class="toolbar">
      <div class="toolbar-left">
        <el-input
          v-model="filter.keyword"
          placeholder="搜索主域名 / 子域名 / 记录值"
          clearable
          style="width:260px"
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

    <!-- 域名表格：展开行内嵌子域名解析记录，默认全部展开 -->
    <el-table
      v-loading="loading"
      :data="tableData"
      stripe
      :row-key="(row) => String(row.id)"
      :expand-row-keys="expandedKeys"
      @expand-change="handleExpandChange"
    >
      <el-table-column type="expand" width="40">
        <template #default="{ row }">
          <div class="records-panel">
            <div class="records-head">
              <span class="records-title">
                {{ appliedKeyword ? '匹配的子域名解析' : '子域名解析' }}（{{ recordsMap[row.id]?.length ?? '…' }}）
                <el-button
                  v-if="appliedKeyword"
                  link
                  type="primary"
                  size="small"
                  @click="clearKeyword"
                >显示全部</el-button>
              </span>
              <el-button
                v-if="authStore.isStaff"
                size="small"
                type="primary"
                :icon="Plus"
                @click="openRecordDialog(row)"
              >新增记录</el-button>
            </div>
            <el-table
              v-loading="!recordsMap[row.id]"
              :data="recordsMap[row.id] || []"
              size="small"
              :show-header="true"
            >
              <el-table-column label="子域名" min-width="200">
                <template #default="{ row: rec }">
                  <span class="fqdn">{{ rec.fqdn }}</span>
                </template>
              </el-table-column>
              <el-table-column prop="type" label="类型" width="80">
                <template #default="{ row: rec }">
                  <el-tag size="small" type="info">{{ rec.type }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="value" label="记录值" min-width="180" show-overflow-tooltip />
              <el-table-column prop="ttl" label="TTL" width="70" />
              <el-table-column label="绑定" min-width="110">
                <template #default="{ row: rec }">
                  <template v-if="rec.target_type === 'server'">
                    <el-tag size="small" type="success">服务器</el-tag>
                    <span style="margin-left:4px">{{ rec.server_name }}</span>
                  </template>
                  <el-tag v-else-if="rec.target_type !== 'none'" size="small" type="warning">
                    {{ TARGET_LABELS[rec.target_type] }}
                  </el-tag>
                  <span v-else class="muted">—</span>
                </template>
              </el-table-column>
              <el-table-column label="启用" width="70">
                <template #default="{ row: rec }">
                  <el-tag size="small" :type="rec.enabled ? 'success' : 'info'">
                    {{ rec.enabled ? '启用' : '暂停' }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="同步" width="100">
                <template #default="{ row: rec }">
                  <el-tooltip v-if="rec.sync_status === 'failed'" :content="rec.sync_error" placement="top">
                    <el-tag size="small" type="danger">推送失败</el-tag>
                  </el-tooltip>
                  <el-tag v-else-if="rec.sync_status === 'pending'" size="small" type="warning">待推送</el-tag>
                  <el-tag v-else size="small" type="success">已同步</el-tag>
                </template>
              </el-table-column>
              <el-table-column v-if="authStore.isStaff" label="操作" width="220">
                <template #default="{ row: rec }">
                  <el-button
                    v-if="rec.sync_status !== 'synced'"
                    size="small"
                    type="warning"
                    :loading="retryingId === rec.id"
                    @click="handleRetry(row, rec)"
                  >重推</el-button>
                  <el-button size="small" :icon="Edit" @click="openRecordDialog(row, rec)">编辑</el-button>
                  <el-button size="small" type="danger" :icon="Delete" @click="handleRecordDelete(row, rec)">删除</el-button>
                </template>
              </el-table-column>
            </el-table>
          </div>
        </template>
      </el-table-column>
      <el-table-column prop="name" label="域名" min-width="180">
        <template #default="{ row }">
          <span class="fqdn">{{ row.name }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="cloud_account_name" label="云账号" min-width="120" />
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
      <el-table-column prop="remark" label="备注" min-width="100" show-overflow-tooltip />
      <el-table-column v-if="authStore.isStaff" label="操作" width="230" fixed="right">
        <template #default="{ row }">
          <el-button size="small" :loading="syncingId === row.id" @click="handleSync(row)">同步</el-button>
          <el-button size="small" :icon="Edit" @click="openDialog(row)">编辑</el-button>
          <el-button size="small" type="danger" :icon="Delete" @click="handleDelete(row)">删除</el-button>
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

  <!-- 新增/编辑解析记录弹窗 -->
  <el-dialog v-model="recordDialogVisible" :title="editRecord ? '编辑解析记录' : '新增解析记录'" width="540px">
    <el-form ref="recordFormRef" :model="recordForm" :rules="recordRules" label-width="90px">
      <el-form-item label="主机记录" prop="rr">
        <el-input v-model="recordForm.rr" placeholder="如 www、@、*">
          <template #append>.{{ recordDomain?.name }}</template>
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

// 展开行：默认全部展开（可收放）；子域名记录按域名 id 缓存
const expandedKeys = ref([])
const recordsMap = reactive({})
// 本次列表实际生效的搜索词（决定展开面板是全量还是仅匹配项）
const appliedKeyword = ref('')

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
    // 搜索词变化时记录缓存失效（面板内容随搜索在「全量/仅匹配」间切换）
    const keywordChanged = appliedKeyword.value !== (filter.keyword || '')
    appliedKeyword.value = filter.keyword || ''
    if (keywordChanged) Object.keys(recordsMap).forEach((k) => delete recordsMap[k])
    // 默认展开当前页全部域名并加载各自记录
    expandedKeys.value = tableData.value.map((d) => String(d.id))
    tableData.value.forEach((d) => loadRecords(d.id))
  } finally {
    loading.value = false
  }
}

// 加载某域名的解析记录（page_size=0 全量，force 强制刷新缓存）
// 有搜索词时只取命中的子域名，便于在大量记录中定位
async function loadRecords(domainId, force = false) {
  if (!force && recordsMap[domainId]) return
  const params = { domain: domainId, page_size: 0 }
  if (appliedKeyword.value) params.keyword = appliedKeyword.value
  const res = await getDnsRecords(params)
  recordsMap[domainId] = res.data?.results || []
}

function clearKeyword() {
  filter.keyword = ''
  fetchData()
}

function handleExpandChange(row, expandedRows) {
  expandedKeys.value = expandedRows.map((r) => String(r.id))
  if (expandedRows.includes(row)) loadRecords(row.id)
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
    await loadRecords(row.id, true)
    fetchData()
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

// ---------- 解析记录增删改 ----------
const recordDialogVisible = ref(false)
const recordSubmitting = ref(false)
const recordDomain = ref(null)
const editRecord = ref(null)
const recordFormRef = ref()
const serverOptions = ref([])
const retryingId = ref(null)

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

function openRecordDialog(domainRow, rec = null) {
  recordDomain.value = domainRow
  editRecord.value = rec
  Object.assign(recordForm, rec ? { ...rec } : emptyRecordForm())
  recordDialogVisible.value = true
  recordFormRef.value?.clearValidate()
}

async function handleRecordSubmit() {
  await recordFormRef.value.validate()
  recordSubmitting.value = true
  try {
    const payload = { ...recordForm, domain: recordDomain.value.id }
    if (editRecord.value) {
      await updateDnsRecord(editRecord.value.id, payload)
      ElMessage.success('更新成功')
    } else {
      await createDnsRecord(payload)
      ElMessage.success('新增成功')
    }
    recordDialogVisible.value = false
    await loadRecords(recordDomain.value.id, true)
    fetchData()
  } finally {
    recordSubmitting.value = false
  }
}

async function handleRecordDelete(domainRow, rec) {
  await ElMessageBox.confirm(
    `确定删除解析记录「${rec.fqdn} ${rec.type}」吗？将同步删除厂商侧解析，立即生效。`,
    '警告',
    { type: 'warning' },
  )
  await deleteDnsRecord(rec.id)
  ElMessage.success('删除成功')
  await loadRecords(domainRow.id, true)
  fetchData()
}

async function handleRetry(domainRow, rec) {
  retryingId.value = rec.id
  try {
    const res = await retrySyncDnsRecord(rec.id)
    ElMessage.success(res.msg || '重推成功')
    await loadRecords(domainRow.id, true)
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
.fqdn { font-weight: 600; }
.records-panel { padding: 4px 16px 12px 48px; background: #fafafa; }
.records-head { display: flex; justify-content: space-between; align-items: center; padding: 8px 0; }
.records-title { font-size: 13px; color: #606266; font-weight: 600; }
</style>
