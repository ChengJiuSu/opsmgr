<template>
  <el-card shadow="never">
    <div class="toolbar">
      <span class="hint">
        内部脚本 / Webhook 免登录调用平台接口的凭证。校验链：API Key → 来源 IP 白名单 →
        作用域 → 可选 Webhook 签名，任一不过即拒。Key 明文仅在创建 / 轮换时展示一次。
      </span>
      <div>
        <el-button :icon="Refresh" @click="fetchData">刷新</el-button>
        <el-button type="primary" :icon="Plus" @click="openDialog()">新增凭证</el-button>
      </div>
    </div>

    <el-table v-loading="loading" :data="tableData" stripe>
      <el-table-column prop="name" label="名称" min-width="130" />
      <el-table-column prop="key_id" label="凭证标识" width="190" show-overflow-tooltip />
      <el-table-column label="作用域" min-width="180">
        <template #default="{ row }">
          <el-tag v-for="s in row.scopes" :key="s" size="small" class="tag-gap">
            {{ scopeLabel(s) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="来源 IP" min-width="170">
        <template #default="{ row }">
          <el-tag v-if="row.ip_unrestricted" size="small" type="danger">不限制</el-tag>
          <span v-else class="mono">{{ row.ip_whitelist }}</span>
        </template>
      </el-table-column>
      <el-table-column label="数据范围" min-width="150">
        <template #default="{ row }">
          <span v-if="row.allowed_project_groups?.length || row.allowed_environments?.length">
            {{ dimLabel(row) }}
          </span>
          <span v-else class="muted">不限</span>
        </template>
      </el-table-column>
      <el-table-column label="签名" width="110">
        <template #default="{ row }">
          <el-tag v-if="row.sig_mode === 'none'" size="small" type="info">不验签</el-tag>
          <el-tag v-else size="small" type="success">
            {{ row.sig_mode === 'token' ? '共享 Token' : 'HMAC' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="限速" width="90">
        <template #default="{ row }">
          <span :class="{ muted: !row.rate_limit }">{{ row.rate_limit || '默认' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="最近使用" width="230">
        <template #default="{ row }">
          <template v-if="row.last_used_at">
            {{ formatTime(row.last_used_at) }}
            <span class="mono muted"> {{ row.last_used_ip }}</span>
          </template>
          <span v-else class="muted">从未</span>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tag size="small" :type="statusType(row)">{{ statusLabel(row) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="remark" label="备注" min-width="120" show-overflow-tooltip>
        <template #default="{ row }">{{ row.remark || '—' }}</template>
      </el-table-column>
      <el-table-column label="操作" width="290" fixed="right">
        <template #default="{ row }">
          <el-button size="small" :icon="Edit" @click="openDialog(row)">编辑</el-button>
          <el-button size="small" type="warning" @click="handleRotate(row)">轮换 Key</el-button>
          <el-button size="small" :type="row.enabled ? 'info' : 'success'" @click="handleToggle(row)">
            {{ row.enabled ? '停用' : '启用' }}
          </el-button>
          <el-button size="small" type="danger" :icon="Delete" @click="handleDelete(row)" />
        </template>
      </el-table-column>
    </el-table>
  </el-card>

  <!-- 新增 / 编辑 -->
  <el-dialog v-model="dialogVisible" :title="editRow ? '编辑 API 凭证' : '新增 API 凭证'" width="620px">
    <el-form ref="formRef" :model="form" :rules="rules" label-width="110px">
      <el-form-item label="凭证名称" prop="name">
        <el-input v-model="form.name" placeholder="如 ci-jenkins、github-webhook" />
        <div v-if="!editRow" class="tip">未指定服务账号时自动创建 svc-&lt;名称&gt; 作为审计身份</div>
      </el-form-item>
      <el-form-item label="作用域" prop="scopes">
        <el-select v-model="form.scopes" multiple placeholder="按最小权限选，只给它真的要调的接口" style="width:100%">
          <el-option v-for="o in scopes" :key="o.value" :label="o.label" :value="o.value" />
        </el-select>
      </el-form-item>

      <el-divider content-position="left">来源限制</el-divider>
      <el-form-item label="不限制 IP">
        <el-switch v-model="form.ip_unrestricted" :disabled="hasHighRiskScope" />
        <span class="tip danger" v-if="hasHighRiskScope">
          已选高敏作用域（读敏感变量真值），必须限定来源 IP，不允许「不限制」
        </span>
        <span class="tip danger" v-else-if="form.ip_unrestricted">
          该 Key 可从任意公网地址使用，泄露即全开，仅在来源确实无法枚举时选择
        </span>
      </el-form-item>
      <el-form-item v-if="!form.ip_unrestricted" label="IP 白名单" prop="ip_whitelist">
        <el-input
          v-model="form.ip_whitelist" type="textarea" :rows="3"
          placeholder="IP 或 CIDR，逗号 / 换行分隔，如&#10;10.0.1.0/24&#10;203.0.113.9"
        />
        <div class="tip">
          按平台看到的真实客户端 IP 匹配（经可信代理链解析，伪造的 X-Forwarded-For 无效）
        </div>
      </el-form-item>

      <el-divider content-position="left">数据范围（可选，留空 = 不限）</el-divider>
      <el-form-item label="项目组">
        <el-select v-model="form.allowed_project_groups" multiple placeholder="留空 = 不限" style="width:100%">
          <el-option v-for="o in dictOptions.projects" :key="o.id" :label="o.name" :value="o.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="环境">
        <el-select v-model="form.allowed_environments" multiple placeholder="留空 = 不限" style="width:100%">
          <el-option v-for="o in dictOptions.envs" :key="o.id" :label="o.name" :value="o.id" />
        </el-select>
        <div class="tip">
          纯收窄，配不出比作用域更宽的权限。主机清单按范围<b>收窄可见集</b>（不是拒绝请求），
          变量解析与制品上报/下载则按范围<b>拒绝</b>越界维度。
        </div>
      </el-form-item>

      <el-divider content-position="left">Webhook 签名（可选）</el-divider>
      <el-form-item label="签名方式">
        <el-select v-model="form.sig_mode" style="width:100%" @change="onSigModeChange">
          <el-option label="不验签（内部脚本，靠 Key + IP 白名单）" value="none" />
          <el-option label="HMAC-SHA256（GitHub / Gitee 风格，签请求体）" value="hmac_sha256" />
          <el-option label="共享 Token（GitLab 风格，比对请求头明文）" value="token" />
        </el-select>
      </el-form-item>
      <template v-if="form.sig_mode !== 'none'">
        <el-form-item label="签名请求头" prop="sig_header">
          <el-input v-model="form.sig_header" placeholder="如 X-Hub-Signature-256 / X-Gitlab-Token" />
        </el-form-item>
        <el-form-item v-if="form.sig_mode === 'hmac_sha256'" label="签名值前缀">
          <el-input v-model="form.sig_prefix" placeholder="如 sha256=；纯 hex 留空" />
        </el-form-item>
        <el-form-item label="签名密钥" :prop="editRow && editRow.has_signing_secret ? '' : 'signing_secret'">
          <el-input
            v-model="form.signing_secret" type="password" show-password
            :placeholder="editRow && editRow.has_signing_secret ? '已设置，留空保持不变' : '至少 16 个字符，与提供方配置一致'"
          />
        </el-form-item>
      </template>

      <el-divider content-position="left">其他</el-divider>
      <el-form-item label="限速">
        <el-input v-model="form.rate_limit" placeholder="如 60/min；留空用全局默认" style="width:200px" />
      </el-form-item>
      <el-form-item label="过期时间">
        <el-date-picker
          v-model="form.expires_at" type="datetime" placeholder="留空长期有效"
          value-format="YYYY-MM-DDTHH:mm:ss" style="width:220px"
        />
      </el-form-item>
      <el-form-item label="启用">
        <el-switch v-model="form.enabled" />
      </el-form-item>
      <el-form-item label="备注">
        <el-input v-model="form.remark" placeholder="如所属脚本 / 负责人" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="dialogVisible = false">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="handleSubmit">确定</el-button>
    </template>
  </el-dialog>

  <!-- 一次性 Key 展示 -->
  <el-dialog v-model="keyVisible" title="API Key（仅展示一次）" width="640px" :close-on-click-modal="false">
    <el-alert type="warning" :closable="false" show-icon style="margin-bottom:12px">
      请立即复制并写入调用方配置，关闭后不再展示；丢了只能轮换重发。
    </el-alert>
    <el-input :model-value="oneTimeKey" readonly>
      <template #append>
        <el-button @click="copyKey">复制</el-button>
      </template>
    </el-input>
    <div class="curl">调用示例：curl -H "X-API-KEY: {{ oneTimeKey }}" &lt;接口地址&gt;</div>
    <template #footer>
      <el-button type="primary" @click="keyVisible = false">我已保存</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { Plus, Edit, Delete, Refresh } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  getApiCredentials, getApiScopes, createApiCredential, updateApiCredential,
  deleteApiCredential, rotateApiCredentialKey, toggleApiCredential,
} from '../../api/apikeys'
import { fetchDictOptions, projectGroupApi, environmentApi } from '../../api/assets'

// 高敏作用域：与后端 apikey.HIGH_RISK_SCOPES 对齐，禁止「不限制来源 IP」
const HIGH_RISK_SCOPES = ['variable:secret']

const loading = ref(false)
const submitting = ref(false)
const tableData = ref([])
const scopes = ref([])

const dialogVisible = ref(false)
const editRow = ref(null)
const formRef = ref()
const form = reactive({
  name: '', scopes: [], ip_unrestricted: false, ip_whitelist: '',
  allowed_project_groups: [], allowed_environments: [],
  sig_mode: 'none', sig_header: '', sig_prefix: '', signing_secret: '',
  rate_limit: '', expires_at: null, enabled: true, remark: '',
})
const dictOptions = reactive({ projects: [], envs: [] })

const hasHighRiskScope = computed(() => form.scopes.some((s) => HIGH_RISK_SCOPES.includes(s)))

// 白名单留空必须显式勾选「不限制」——与后端同一条规则，前端先挡一次给出明确提示
const rules = {
  name: [{ required: true, message: '请输入凭证名称' }],
  scopes: [{ required: true, message: '请至少选择一个作用域' }],
  ip_whitelist: [{
    validator: (_rule, value, callback) => {
      if (form.ip_unrestricted || (value || '').trim()) return callback()
      callback(new Error('请填写 IP 白名单；确实不限制来源请勾选上方「不限制 IP」'))
    },
  }],
  sig_header: [{ required: true, message: '请填写签名请求头' }],
  signing_secret: [{ required: true, message: '请设置签名密钥（至少 16 个字符）' }],
}

const keyVisible = ref(false)
const oneTimeKey = ref('')

const formatTime = (t) => (t ? t.slice(0, 19).replace('T', ' ') : '')
const scopeLabel = (value) => scopes.value.find((s) => s.value === value)?.label || value
const dictName = (list, id) => list.find((o) => o.id === id)?.name || id
const dimLabel = (row) => {
  const parts = []
  if (row.allowed_project_groups?.length) {
    parts.push(row.allowed_project_groups.map((id) => dictName(dictOptions.projects, id)).join('、'))
  }
  if (row.allowed_environments?.length) {
    parts.push(row.allowed_environments.map((id) => dictName(dictOptions.envs, id)).join('、'))
  }
  return parts.join(' / ')
}
const statusType = (row) => (row.expired ? 'danger' : row.enabled ? 'success' : 'info')
const statusLabel = (row) => (row.expired ? '已过期' : row.enabled ? '启用' : '停用')

async function fetchData() {
  loading.value = true
  try {
    const res = await getApiCredentials()
    tableData.value = Array.isArray(res) ? res : res.data || []
  } finally {
    loading.value = false
  }
}

async function loadScopes() {
  const res = await getApiScopes()
  scopes.value = res.data || []
}

function onSigModeChange(mode) {
  if (mode === 'none') {
    form.sig_header = ''
    form.sig_prefix = ''
    form.signing_secret = ''
  } else if (!form.sig_header) {
    form.sig_header = mode === 'token' ? 'X-Gitlab-Token' : 'X-Hub-Signature-256'
    form.sig_prefix = mode === 'token' ? '' : 'sha256='
  }
  formRef.value?.clearValidate(['sig_header', 'signing_secret'])
}

function openDialog(row = null) {
  editRow.value = row
  Object.assign(form, {
    name: row?.name || '',
    scopes: row?.scopes ? [...row.scopes] : [],
    ip_unrestricted: row?.ip_unrestricted || false,
    ip_whitelist: row?.ip_whitelist || '',
    allowed_project_groups: row?.allowed_project_groups ? [...row.allowed_project_groups] : [],
    allowed_environments: row?.allowed_environments ? [...row.allowed_environments] : [],
    sig_mode: row?.sig_mode || 'none',
    sig_header: row?.sig_header || '',
    sig_prefix: row?.sig_prefix || '',
    signing_secret: '',  // 密钥不回显，留空表示保持不变
    rate_limit: row?.rate_limit || '',
    expires_at: row?.expires_at ? row.expires_at.slice(0, 19) : null,
    enabled: row ? row.enabled : true,
    remark: row?.remark || '',
  })
  dialogVisible.value = true
  formRef.value?.clearValidate()
}

async function handleSubmit() {
  await formRef.value.validate()
  submitting.value = true
  try {
    const payload = { ...form, expires_at: form.expires_at || null }
    if (hasHighRiskScope.value) payload.ip_unrestricted = false
    if (!payload.signing_secret) delete payload.signing_secret  // 留空 = 保持原密钥
    if (editRow.value) {
      await updateApiCredential(editRow.value.id, payload)
      ElMessage.success('更新成功')
    } else {
      const res = await createApiCredential(payload)
      oneTimeKey.value = res.data?.api_key || ''
      keyVisible.value = true
    }
    dialogVisible.value = false
    fetchData()
  } finally {
    submitting.value = false
  }
}

async function handleRotate(row) {
  await ElMessageBox.confirm(
    `确定轮换「${row.name}」的 Key 吗？旧 Key 立即失效，所有还在用它的脚本 / Webhook 会开始报 401，`
    + `需同步更新其配置。`,
    '警告', { type: 'warning' },
  )
  const res = await rotateApiCredentialKey(row.id)
  oneTimeKey.value = res.data?.api_key || ''
  keyVisible.value = true
  fetchData()
}

async function handleToggle(row) {
  if (row.enabled) {
    await ElMessageBox.confirm(
      `确定停用「${row.name}」吗？用它调用的脚本 / Webhook 会立即开始失败（停用可随时恢复，泄露时优先用它）。`,
      '警告', { type: 'warning' },
    )
  }
  await toggleApiCredential(row.id)
  ElMessage.success(row.enabled ? '已停用' : '已启用')
  fetchData()
}

async function handleDelete(row) {
  await ElMessageBox.confirm(
    `确定删除「${row.name}」吗？删除后无法恢复，用它调用的脚本 / Webhook 会立即失败；`
    + `只是想临时切断请用「停用」。`,
    '警告', { type: 'warning', confirmButtonText: '删除', confirmButtonClass: 'el-button--danger' },
  )
  await deleteApiCredential(row.id)
  ElMessage.success('已删除')
  fetchData()
}

async function copyKey() {
  try {
    await navigator.clipboard.writeText(oneTimeKey.value)
    ElMessage.success('已复制')
  } catch {
    ElMessage.warning('复制失败，请手动选择复制')
  }
}

async function loadDicts() {
  const [proj, env] = await Promise.all([
    fetchDictOptions(projectGroupApi), fetchDictOptions(environmentApi),
  ])
  dictOptions.projects = proj.data?.results || []
  dictOptions.envs = env.data?.results || []
}

onMounted(() => {
  fetchData()
  loadScopes()
  loadDicts()
})
</script>

<style scoped>
.toolbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; gap: 12px; }
.hint { color: #909399; font-size: 13px; }
.muted { color: #c0c4cc; }
.mono { font-family: Consolas, Monaco, monospace; font-size: 12px; }
.tag-gap { margin-right: 4px; }
.tip { color: #909399; font-size: 12px; line-height: 1.6; }
.tip.danger { color: var(--el-color-danger); margin-left: 8px; }
.curl { margin-top: 10px; color: #909399; font-size: 12px; word-break: break-all; font-family: Consolas, Monaco, monospace; }
</style>
