<template>
  <el-card shadow="never">
    <!-- 工具栏 -->
    <div class="toolbar">
      <div class="toolbar-left">
        <el-input
          v-model="filter.keyword"
          placeholder="搜索证书名称"
          clearable
          style="width:200px"
          @keyup.enter="fetchData"
          @clear="fetchData"
        />
        <el-select v-model="filter.status" placeholder="状态" clearable style="width:130px" @change="fetchData">
          <el-option v-for="(label, key) in STATUS_LABELS" :key="key" :label="label" :value="key" />
        </el-select>
      </div>
      <el-button v-if="authStore.isStaff" type="primary" :icon="Plus" @click="openDialog()">新增证书</el-button>
    </div>

    <!-- 表格 -->
    <el-table v-loading="loading" :data="tableData" stripe>
      <el-table-column prop="name" label="证书名称" min-width="130" />
      <el-table-column label="域名" min-width="220">
        <template #default="{ row }">
          <el-tag v-for="d in row.domains" :key="d" size="small" type="info" style="margin-right:4px">
            {{ d }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="cloud_account_name" label="验证账号" min-width="120" />
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tooltip v-if="row.status === 'failed'" :content="row.last_error" placement="top">
            <el-tag size="small" type="danger">申请失败</el-tag>
          </el-tooltip>
          <el-tag v-else size="small" :type="STATUS_TAG[row.status]">
            {{ STATUS_LABELS[row.status] }}
            <el-icon v-if="row.status === 'issuing'" class="is-loading" style="margin-left:2px"><Loading /></el-icon>
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="有效期" width="180">
        <template #default="{ row }">
          <template v-if="row.expires_at">
            <span>{{ row.expires_at.slice(0, 10) }}</span>
            <el-tag
              size="small"
              :type="row.days_left <= 14 ? 'danger' : row.days_left <= 30 ? 'warning' : 'success'"
              style="margin-left:6px"
            >{{ row.days_left }} 天</el-tag>
          </template>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column label="自动续签" width="90">
        <template #default="{ row }">
          <el-switch
            v-if="authStore.isStaff"
            :model-value="row.auto_renew"
            @change="(v) => toggleAutoRenew(row, v)"
          />
          <el-tag v-else size="small" :type="row.auto_renew ? 'success' : 'info'">
            {{ row.auto_renew ? '开' : '关' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="remark" label="备注" min-width="100" show-overflow-tooltip />
      <el-table-column v-if="authStore.isStaff" label="操作" width="310" fixed="right">
        <template #default="{ row }">
          <el-button
            v-if="['pending', 'failed'].includes(row.status)"
            size="small"
            type="primary"
            @click="handleIssue(row)"
          >申请</el-button>
          <el-button
            v-if="row.status === 'issued'"
            size="small"
            type="warning"
            @click="handleRenew(row)"
          >续签</el-button>
          <el-button
            v-if="row.status === 'issued'"
            size="small"
            :icon="Download"
            @click="handleDownload(row)"
          >下载</el-button>
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

  <!-- 新增/编辑弹窗 -->
  <el-dialog v-model="dialogVisible" :title="editRow ? '编辑证书' : '新增证书'" width="560px">
    <el-form ref="formRef" :model="form" :rules="rules" label-width="100px">
      <el-form-item label="证书名称" prop="name">
        <el-input v-model="form.name" placeholder="如 example-wildcard" />
      </el-form-item>
      <el-form-item label="域名列表" prop="domains">
        <el-select
          v-model="form.domains"
          multiple
          filterable
          allow-create
          default-first-option
          :disabled="domainsLocked"
          placeholder="输入后回车添加，支持通配符 *.example.com"
          style="width:100%"
        >
          <el-option v-for="d in domainSuggestions" :key="d" :label="d" :value="d" />
        </el-select>
        <div class="tip">
          首个域名为证书主域名（不能是通配符）；同一证书可含多个域名（SAN）。
          <template v-if="domainsLocked">已签发证书域名锁定，改域名请新建证书。</template>
        </div>
      </el-form-item>
      <el-form-item label="验证账号" prop="cloud_account">
        <el-select v-model="form.cloud_account" :disabled="domainsLocked" style="width:100%">
          <el-option
            v-for="a in acmeAccountOptions"
            :key="a.id"
            :label="`${a.name}（${a.provider_name}）`"
            :value="a.id"
          />
        </el-select>
        <div class="tip">DNS 验证走该账号的解析服务；当前支持阿里云、腾讯云</div>
      </el-form-item>
      <el-form-item label="自动续签">
        <el-switch v-model="form.auto_renew" />
        <span class="tip" style="margin-left:8px">距过期不足 30 天时后台自动重签</span>
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
</template>

<script setup>
import { ref, reactive, computed, onMounted, onUnmounted } from 'vue'
import { Plus, Edit, Delete, Download, Loading } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useAuthStore } from '../../stores/auth'
import {
  getCloudAccounts, getDomains,
  getCertificates, createCertificate, updateCertificate, deleteCertificate,
  issueCertificate, renewCertificate, downloadCertificate,
} from '../../api/cloud'

const authStore = useAuthStore()

const STATUS_LABELS = { pending: '待申请', issuing: '申请中', issued: '已签发', failed: '申请失败', expired: '已过期' }
const STATUS_TAG = { pending: 'info', issuing: 'warning', issued: 'success', failed: 'danger', expired: 'danger' }
// DNS 验证支持的厂商（与后端 ACME_DNS_HOOKS 对应）
const ACME_PROVIDERS = ['aliyun', 'tencent']

const loading = ref(false)
const submitting = ref(false)
const tableData = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const filter = reactive({ keyword: '', status: '' })
const accountOptions = ref([])
const domainSuggestions = ref([])
const dialogVisible = ref(false)
const editRow = ref(null)
const formRef = ref()

const acmeAccountOptions = computed(() =>
  accountOptions.value.filter((a) => ACME_PROVIDERS.includes(a.provider_code)),
)
// 已签发证书锁定域名与验证账号（后端同样校验）
const domainsLocked = computed(() =>
  editRow.value && !['pending', 'failed'].includes(editRow.value.status),
)

const emptyForm = () => ({ name: '', domains: [], cloud_account: null, auto_renew: true, remark: '' })
const form = reactive(emptyForm())
const rules = {
  name: [{ required: true, message: '请输入证书名称' }],
  domains: [{ required: true, type: 'array', min: 1, message: '请至少填写一个域名' }],
  cloud_account: [{ required: true, message: '请选择验证账号' }],
}

async function fetchData() {
  loading.value = true
  try {
    const params = { page: page.value, page_size: pageSize.value }
    if (filter.keyword) params.keyword = filter.keyword
    if (filter.status) params.status = filter.status
    const res = await getCertificates(params)
    tableData.value = res.data?.results || []
    total.value = res.data?.total || 0
  } finally {
    loading.value = false
  }
  schedulePoll()
}

// 存在申请中的证书时 5s 轮询刷新
let pollTimer = null
function schedulePoll() {
  clearTimeout(pollTimer)
  if (tableData.value.some((r) => r.status === 'issuing')) {
    pollTimer = setTimeout(fetchData, 5000)
  }
}
onUnmounted(() => clearTimeout(pollTimer))

function openDialog(row = null) {
  editRow.value = row
  Object.assign(form, row ? { ...row } : emptyForm())
  dialogVisible.value = true
  formRef.value?.clearValidate()
}

async function handleSubmit() {
  await formRef.value.validate()
  submitting.value = true
  try {
    if (editRow.value) {
      await updateCertificate(editRow.value.id, form)
      ElMessage.success('更新成功')
    } else {
      await createCertificate(form)
      ElMessage.success('新增成功，点击「申请」开始签发')
    }
    dialogVisible.value = false
    fetchData()
  } finally {
    submitting.value = false
  }
}

async function handleIssue(row) {
  await ElMessageBox.confirm(
    `确定为「${row.name}」申请证书吗？将通过 ${row.cloud_account_name} 的 DNS 添加验证记录（验证完成自动删除），耗时约 2~5 分钟。`,
    '申请证书',
    { type: 'info' },
  )
  const res = await issueCertificate(row.id)
  ElMessage.success(res.msg || '已提交申请')
  fetchData()
}

async function handleRenew(row) {
  await ElMessageBox.confirm(
    `确定续签「${row.name}」吗？将强制重签，签发与过期时间刷新；已部署副本需重新分发生效。`,
    '续签证书',
    { type: 'warning' },
  )
  const res = await renewCertificate(row.id)
  ElMessage.success(res.msg || '已提交续签')
  fetchData()
}

// 下载证书材料：cert / key / fullchain 三个文件打包为浏览器逐个下载
async function handleDownload(row) {
  const res = await downloadCertificate(row.id)
  const data = res.data
  const files = [
    [`${data.name}.crt`, data.cert_pem],
    [`${data.name}.key`, data.key_pem],
    [`${data.name}.fullchain.pem`, data.fullchain_pem],
  ]
  for (const [filename, content] of files) {
    if (!content) continue
    const blob = new Blob([content], { type: 'application/x-pem-file' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    a.click()
    URL.revokeObjectURL(url)
  }
  ElMessage.success('已下载证书、私钥与完整链')
}

async function toggleAutoRenew(row, value) {
  try {
    await updateCertificate(row.id, { auto_renew: value })
    row.auto_renew = value
  } catch {
    fetchData()
  }
}

async function handleDelete(row) {
  await ElMessageBox.confirm(
    `确定删除证书「${row.name}」吗？删除后本平台不再续签，已部署到服务器的副本不受影响。`,
    '警告',
    { type: 'warning' },
  )
  await deleteCertificate(row.id)
  ElMessage.success('删除成功')
  fetchData()
}

onMounted(async () => {
  fetchData()
  if (authStore.isStaff) {
    const [accRes, domRes] = await Promise.all([
      getCloudAccounts({ page_size: 0, enabled: 'true' }),
      getDomains({ page_size: 0 }),
    ])
    accountOptions.value = accRes.data?.results || []
    // 已管理域名作为下拉建议（可自由输入其他域名/通配符）
    domainSuggestions.value = (domRes.data?.results || []).flatMap((d) => [d.name, `*.${d.name}`])
  }
})
</script>

<style scoped>
.toolbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.toolbar-left { display: flex; gap: 8px; }
.muted { color: #909399; }
.tip { font-size: 12px; color: #909399; line-height: 1.6; }
</style>
