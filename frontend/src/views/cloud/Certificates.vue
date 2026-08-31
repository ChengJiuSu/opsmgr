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
          @input="searchDebounced"
          @keyup.enter="handleSearch"
          @clear="handleSearch"
        />
        <el-select v-model="filter.status" placeholder="状态" clearable style="width:130px" @change="handleSearch">
          <el-option v-for="(label, key) in STATUS_LABELS" :key="key" :label="label" :value="key" />
        </el-select>
      </div>
      <el-button v-if="canWrite" type="primary" :icon="Plus" @click="openDialog()">新增证书</el-button>
    </div>

    <!-- 从概览页的过期告警点进来时提示当前视图被收窄了，可一键清除 -->
    <el-alert
      v-if="filter.expiringDays"
      type="warning"
      :closable="false"
      show-icon
      class="scoped-alert"
    >
      仅显示 {{ filter.expiringDays }} 天内到期（含已过期）的证书，按到期时间升序。
      <el-button link type="primary" @click="clearExpiring">显示全部</el-button>
    </el-alert>

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
          <el-tooltip
            v-else-if="row.issue_stuck"
            content="申请超过 30 分钟未回执，执行进程可能已重启。可重新申请或删除。"
            placement="top"
          >
            <el-tag size="small" type="danger">申请超时</el-tag>
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
            v-if="canWrite"
            :model-value="row.auto_renew"
            @change="(v) => toggleAutoRenew(row, v)"
          />
          <el-tag v-else size="small" :type="row.auto_renew ? 'success' : 'info'">
            {{ row.auto_renew ? '开' : '关' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="分发目标" width="90">
        <template #default="{ row }">
          <el-link type="primary" :underline="false" @click="openDeployments(row)">
            {{ row.deployment_count }} 个
          </el-link>
        </template>
      </el-table-column>
      <el-table-column prop="remark" label="备注" min-width="100" show-overflow-tooltip />
      <el-table-column v-if="canWrite" label="操作" width="380" fixed="right">
        <template #default="{ row }">
          <el-button
            v-if="['pending', 'failed'].includes(row.status) || row.issue_stuck"
            size="small"
            type="primary"
            @click="handleIssue(row)"
          >{{ row.issue_stuck ? '重新申请' : '申请' }}</el-button>
          <el-button
            v-if="row.status === 'issued'"
            size="small"
            type="warning"
            @click="handleRenew(row)"
          >续签</el-button>
          <el-button size="small" @click="openDeployments(row)">分发</el-button>
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
          <el-option-group v-for="g in domainSuggestions" :key="g.label" :label="g.label">
            <el-option v-for="d in g.options" :key="d" :label="d" :value="d" />
          </el-option-group>
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

  <!-- 分发目标抽屉 -->
  <el-drawer v-model="deployVisible" :title="`证书分发 — ${deployCert?.name || ''}`" size="70%">
    <div class="toolbar">
      <div class="tip">
        分发到服务器：推送 <code>{{ safeName(deployCert?.name) }}.crt / .key / .fullchain.pem</code>
        到部署目录（私钥 0600、覆盖前自动备份），随后执行重载命令。
        开启「自动分发」的目标在证书续签成功后自动重新推送。
      </div>
      <div v-if="canWrite" style="white-space:nowrap">
        <el-button
          :disabled="!deployRows.length || deployCert?.status !== 'issued'"
          :loading="deployingAll"
          @click="handleDeployAll"
        >全部分发</el-button>
        <el-button type="primary" :icon="Plus" @click="openDeployDialog()">新增目标</el-button>
      </div>
    </div>

    <el-table v-loading="deployLoading" :data="deployRows" stripe>
      <el-table-column label="目标" min-width="200">
        <template #default="{ row }">
          <template v-if="row.target_type === 'server'">
            <el-tag size="small" type="success">服务器</el-tag>
            <span style="margin-left:6px">{{ row.server_name }}（{{ row.server_ip }}）</span>
          </template>
          <template v-else>
            <el-tag size="small" type="warning">{{ row.target_type_display }}</el-tag>
            <span class="muted" style="margin-left:6px">暂未接入</span>
          </template>
        </template>
      </el-table-column>
      <el-table-column prop="deploy_path" label="部署目录" min-width="160" show-overflow-tooltip />
      <el-table-column prop="reload_cmd" label="重载命令" min-width="150" show-overflow-tooltip>
        <template #default="{ row }">
          <span v-if="row.reload_cmd">{{ row.reload_cmd }}</span>
          <span v-else class="muted">不执行</span>
        </template>
      </el-table-column>
      <el-table-column prop="credential_name" label="凭据" min-width="110">
        <template #default="{ row }">
          <span v-if="row.credential_name">{{ row.credential_name }}</span>
          <span v-else class="muted">主机默认</span>
        </template>
      </el-table-column>
      <el-table-column label="自动分发" width="90">
        <template #default="{ row }">
          <el-switch
            v-if="canWrite"
            :model-value="row.auto_deploy"
            @change="(v) => toggleAutoDeploy(row, v)"
          />
          <el-tag v-else size="small" :type="row.auto_deploy ? 'success' : 'info'">
            {{ row.auto_deploy ? '开' : '关' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tooltip v-if="row.status === 'failed'" :content="row.last_error" placement="top">
            <el-tag size="small" type="danger">失败</el-tag>
          </el-tooltip>
          <el-tooltip
            v-else-if="row.deploy_stuck"
            content="分发超过 15 分钟未回执，执行进程可能已重启。可重新分发。"
            placement="top"
          >
            <el-tag size="small" type="danger">分发超时</el-tag>
          </el-tooltip>
          <el-tag v-else size="small" :type="DEPLOY_TAG[row.status]">
            {{ DEPLOY_LABELS[row.status] }}
            <el-icon v-if="row.status === 'deploying'" class="is-loading" style="margin-left:2px"><Loading /></el-icon>
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="最近部署" width="160">
        <template #default="{ row }">
          <span v-if="row.last_deployed_at">{{ row.last_deployed_at.replace('T', ' ').slice(0, 19) }}</span>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column v-if="canWrite" label="操作" width="250" fixed="right">
        <template #default="{ row }">
          <el-button
            size="small"
            type="primary"
            :disabled="deployCert?.status !== 'issued' || row.target_type !== 'server'"
            :loading="deployingId === row.id"
            @click="handleDeploy(row)"
          >分发</el-button>
          <el-button v-if="row.last_output" size="small" @click="showOutput(row)">日志</el-button>
          <el-button size="small" :icon="Edit" @click="openDeployDialog(row)">编辑</el-button>
          <el-button size="small" type="danger" :icon="Delete" @click="handleDeployDelete(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>
  </el-drawer>

  <!-- 新增/编辑分发目标弹窗 -->
  <el-dialog v-model="deployDialogVisible" :title="editDeploy ? '编辑分发目标' : '新增分发目标'" width="560px">
    <el-form ref="deployFormRef" :model="deployForm" :rules="deployRules" label-width="100px">
      <el-form-item label="目标类型" prop="target_type">
        <el-select v-model="deployForm.target_type" style="width:100%">
          <el-option
            v-for="(label, key) in DEPLOY_TARGETS"
            :key="key"
            :label="key === 'server' ? label : `${label}（暂未接入）`"
            :value="key"
          />
        </el-select>
      </el-form-item>
      <el-form-item v-if="deployForm.target_type === 'server'" label="服务器" prop="server">
        <el-select v-model="deployForm.server" filterable style="width:100%">
          <el-option
            v-for="s in serverOptions"
            :key="s.id"
            :label="`${s.name}（${s.public_ip || s.private_ip}）`"
            :value="s.id"
          />
        </el-select>
        <div class="tip">按主机执行通道自动选路：直连主机本机推送，agent 通道主机经该 agent 下发（agent 需 v1.2+）</div>
      </el-form-item>
      <el-form-item label="部署目录" prop="deploy_path">
        <el-input v-model="deployForm.deploy_path" placeholder="/etc/nginx/ssl" />
        <div class="tip">
          绝对路径；文件将落为
          <code>{{ deployForm.deploy_path || '/etc/nginx/ssl' }}/{{ safeName(deployCert?.name) }}.crt</code> 等
        </div>
      </el-form-item>
      <el-form-item label="重载命令">
        <el-input v-model="deployForm.reload_cmd" placeholder="如 nginx -t && nginx -s reload（留空不执行）" />
      </el-form-item>
      <el-form-item label="连接凭据">
        <el-select v-model="deployForm.credential" clearable placeholder="留空用主机默认 SSH 用户" style="width:100%">
          <el-option v-for="c in credentialOptions" :key="c.id" :label="c.name" :value="c.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="自动分发">
        <el-switch v-model="deployForm.auto_deploy" />
        <span class="tip" style="margin-left:8px">证书续签成功后自动重新推送</span>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="deployDialogVisible = false">取消</el-button>
      <el-button type="primary" :loading="deploySubmitting" @click="handleDeploySubmit">确定</el-button>
    </template>
  </el-dialog>

  <!-- 分发执行日志 -->
  <el-dialog v-model="outputVisible" title="最近分发输出" width="70%">
    <pre class="output">{{ outputText }}</pre>
  </el-dialog>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onUnmounted } from 'vue'
import { Plus, Edit, Delete, Download, Loading } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useRoute } from 'vue-router'
import { useAuthStore } from '../../stores/auth'
import { debounce } from '../../utils/debounce'
import { getServers } from '../../api/assets'
import { getCredentials } from '../../api/tasks'
import {
  getCloudAccounts, getDomains, getDnsRecords,
  getCertificates, createCertificate, updateCertificate, deleteCertificate,
  issueCertificate, renewCertificate, downloadCertificate,
  getCertDeployments, createCertDeployment, updateCertDeployment,
  deleteCertDeployment, deployCertDeployment, deployCertificateAll,
} from '../../api/cloud'

const route = useRoute()
const authStore = useAuthStore()
// 按钮可见性跟角色矩阵走：域名与证书是 cloud 模块，
// 云账号下拉另按 cloud_account 判（只读档也能选账号，AccessKey 是打码的）
const canWrite = computed(() => authStore.can('cloud', 'write'))
const canAccount = computed(() => authStore.can('cloud_account'))

const STATUS_LABELS = { pending: '待申请', issuing: '申请中', issued: '已签发', failed: '申请失败', expired: '已过期' }
const STATUS_TAG = { pending: 'info', issuing: 'warning', issued: 'success', failed: 'danger', expired: 'danger' }
// DNS 验证支持的厂商（与后端 ACME_DNS_HOOKS 对应）
const ACME_PROVIDERS = ['aliyun', 'tencent']
// 分发目标类型与状态（server 已实现，其余为后端预留桩）
const DEPLOY_TARGETS = { server: '服务器', lb: '负载均衡', ga: '全球加速', cdn: 'CDN' }
const DEPLOY_LABELS = { pending: '待部署', deploying: '部署中', success: '成功', failed: '失败' }
const DEPLOY_TAG = { pending: 'info', deploying: 'warning', success: 'success', failed: 'danger' }

// 证书文件名前缀：与后端 deploy.safe_cert_name / 下载包同口径
function safeName(name) {
  return (name || '').replace(/[^\w.*-]/g, '_')
}

const loading = ref(false)
const submitting = ref(false)
const tableData = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
// expiringDays 只由概览页的过期告警带进来（?expiring_days=30），没有对应的下拉框：
// 它是「从别处点进来看这一批」的视图，不是常驻筛选项，清掉即恢复全量
const filter = reactive({ keyword: '', status: '', expiringDays: '' })
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
    if (filter.expiringDays) params.expiring_days = filter.expiringDays
    const res = await getCertificates(params)
    tableData.value = res.data?.results || []
    total.value = res.data?.total || 0
  } finally {
    loading.value = false
  }
  schedulePoll()
}

// 搜索：回到第一页再查
function handleSearch() {
  searchDebounced.cancel()
  page.value = 1
  fetchData()
}

// 边输边查（300ms 防抖），无需回车
const searchDebounced = debounce(handleSearch)
onUnmounted(() => searchDebounced.cancel())

// 存在申请中的证书时 5s 轮询刷新
let pollTimer = null
function schedulePoll() {
  clearTimeout(pollTimer)
  if (tableData.value.some((r) => r.status === 'issuing' && !r.issue_stuck)) {
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

// 下载证书 zip 包（后端打包 crt/key/fullchain + README）
async function handleDownload(row) {
  const blob = await downloadCertificate(row.id)
  const filename = `${row.name.replace(/[^\w.*-]/g, '_')}.zip`
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
  ElMessage.success('已下载证书压缩包')
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
  const stuckHint = row.issue_stuck
    ? '该证书的申请任务已超时未回执（执行进程可能已重启），可直接删除。'
    : ''
  await ElMessageBox.confirm(
    `确定删除证书「${row.name}」吗？${stuckHint}删除后本平台不再续签，已部署到服务器的副本不受影响。`,
    '警告',
    { type: 'warning' },
  )
  await deleteCertificate(row.id)
  ElMessage.success('删除成功')
  fetchData()
}

// ── 分发目标 ──────────────────────────────────────────────
const deployVisible = ref(false)
const deployLoading = ref(false)
const deployCert = ref(null)
const deployRows = ref([])
const deployingId = ref(null)
const deployingAll = ref(false)
const serverOptions = ref([])
const credentialOptions = ref([])

const deployDialogVisible = ref(false)
const deploySubmitting = ref(false)
const editDeploy = ref(null)
const deployFormRef = ref()
const outputVisible = ref(false)
const outputText = ref('')

const emptyDeployForm = () => ({
  target_type: 'server', server: null, deploy_path: '/etc/nginx/ssl',
  reload_cmd: '', credential: null, auto_deploy: true,
})
const deployForm = reactive(emptyDeployForm())
const deployRules = {
  target_type: [{ required: true, message: '请选择目标类型' }],
  server: [{ required: true, message: '请选择服务器' }],
  deploy_path: [{ required: true, message: '请输入部署目录' }],
}

function openDeployments(row) {
  deployCert.value = row
  deployVisible.value = true
  fetchDeployments()
}

async function fetchDeployments() {
  deployLoading.value = true
  try {
    const res = await getCertDeployments({ certificate: deployCert.value.id, page_size: 0 })
    deployRows.value = res.data?.results || []
  } finally {
    deployLoading.value = false
  }
  scheduleDeployPoll()
}

// 存在部署中的目标时 5s 轮询
let deployTimer = null
function scheduleDeployPoll() {
  clearTimeout(deployTimer)
  if (deployVisible.value && deployRows.value.some((r) => r.status === 'deploying' && !r.deploy_stuck)) {
    deployTimer = setTimeout(fetchDeployments, 5000)
  }
}
onUnmounted(() => clearTimeout(deployTimer))

function openDeployDialog(row = null) {
  editDeploy.value = row
  Object.assign(deployForm, row ? { ...row } : emptyDeployForm())
  deployDialogVisible.value = true
  deployFormRef.value?.clearValidate()
}

async function handleDeploySubmit() {
  await deployFormRef.value.validate()
  deploySubmitting.value = true
  try {
    const payload = { ...deployForm, certificate: deployCert.value.id }
    if (editDeploy.value) {
      await updateCertDeployment(editDeploy.value.id, payload)
      ElMessage.success('更新成功')
    } else {
      await createCertDeployment(payload)
      ElMessage.success('已登记分发目标，点击「分发」推送证书')
    }
    deployDialogVisible.value = false
    fetchDeployments()
    fetchData()
  } finally {
    deploySubmitting.value = false
  }
}

async function handleDeploy(row) {
  await ElMessageBox.confirm(
    `确定将证书推送到 ${row.server_name}（${row.deploy_path}）吗？`
    + (row.reload_cmd ? `推送后执行：${row.reload_cmd}` : '推送后不执行重载命令。'),
    '分发证书',
    { type: 'warning' },
  )
  deployingId.value = row.id
  try {
    const res = await deployCertDeployment(row.id)
    ElMessage.success(res.msg || '已提交分发')
    fetchDeployments()
  } finally {
    deployingId.value = null
  }
}

async function handleDeployAll() {
  await ElMessageBox.confirm(
    `确定分发到全部 ${deployRows.value.length} 个目标吗？未接入的目标类型将跳过。`,
    '全部分发',
    { type: 'warning' },
  )
  deployingAll.value = true
  try {
    const res = await deployCertificateAll(deployCert.value.id)
    ElMessage.success(res.msg || '已提交分发')
    fetchDeployments()
  } finally {
    deployingAll.value = false
  }
}

async function toggleAutoDeploy(row, value) {
  try {
    await updateCertDeployment(row.id, { auto_deploy: value })
    row.auto_deploy = value
  } catch {
    fetchDeployments()
  }
}

function showOutput(row) {
  outputText.value = row.last_output || ''
  outputVisible.value = true
}

async function handleDeployDelete(row) {
  await ElMessageBox.confirm(
    '确定删除该分发目标吗？目标机上已部署的证书文件不受影响。',
    '警告',
    { type: 'warning' },
  )
  await deleteCertDeployment(row.id)
  ElMessage.success('删除成功')
  fetchDeployments()
  fetchData()
}

function clearExpiring() {
  filter.expiringDays = ''
  handleSearch()
}

onMounted(async () => {
  // 概览页告警的落地筛选：?expiring_days=30 / ?status=failed / ?keyword=xxx
  const q = route.query
  if (q.expiring_days) filter.expiringDays = String(q.expiring_days)
  if (q.status) filter.status = String(q.status)
  if (q.keyword) filter.keyword = String(q.keyword)
  fetchData()
  if (canWrite.value) {
    // 下拉选项分属四个模块（云账号 / 服务器 / 连接凭据 / 云控自身），逐个按权限取：
    // Promise.all 一拒全拒，少一个模块的权限不该把整页选项都带走
    const [accRes, domRes, recRes, svrRes, credRes] = await Promise.all([
      canAccount.value ? getCloudAccounts({ page_size: 0, enabled: 'true' }) : null,
      getDomains({ page_size: 0 }),
      getDnsRecords({ page_size: 0 }),
      authStore.can('cmdb') ? getServers({ page_size: 0 }) : null,
      authStore.can('credential') ? getCredentials({ page_size: 0 }) : null,
    ])
    serverOptions.value = svrRes?.data?.results || []
    credentialOptions.value = credRes?.data?.results || []
    accountOptions.value = accRes?.data?.results || []
    // 建议项按主域名分组：主域名 + *.通配 + 该域名下子域名 FQDN（A/AAAA/CNAME），可自由输入
    const records = recRes?.data?.results || []
    domainSuggestions.value = (domRes?.data?.results || []).map((d) => {
      const subs = records
        .filter((r) => r.domain === d.id && ['A', 'AAAA', 'CNAME'].includes(r.type) && r.rr !== '@')
        .map((r) => r.fqdn)
      return { label: d.name, options: [...new Set([d.name, `*.${d.name}`, ...subs])] }
    })
  }
})
</script>

<style scoped>
.scoped-alert { margin-bottom: 12px; }
.toolbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.toolbar-left { display: flex; gap: 8px; }
.muted { color: #909399; }
.tip { font-size: 12px; color: #909399; line-height: 1.6; }
.tip code { background: #f4f4f5; padding: 0 4px; border-radius: 3px; }
.output {
  margin: 0; padding: 12px; background: #1e1e1e; color: #d4d4d4;
  font-size: 12px; line-height: 1.6; border-radius: 4px;
  max-height: 60vh; overflow: auto; white-space: pre-wrap; word-break: break-all;
}
</style>
