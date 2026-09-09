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
          @input="searchDebounced"
          @keyup.enter="handleSearch"
          @clear="handleSearch"
        />
        <el-select v-model="filter.cloudAccount" placeholder="云账号" clearable style="width:180px" @change="handleSearch">
          <el-option v-for="a in accountOptions" :key="a.id" :label="a.name" :value="a.id" />
        </el-select>
      </div>
      <div>
        <!-- 变更审批是「批/驳一条高危变更」的入口（后端要 cloud write + sudo），
             只读档看得到也点不动，藏掉；变更留痕是纯只读，保留 -->
        <el-badge v-if="canWrite" :value="pendingCount" :hidden="!pendingCount" type="warning">
          <el-button :icon="Finished" @click="openRequests()">变更审批</el-button>
        </el-badge>
        <el-button :icon="Clock" @click="openChanges()">变更留痕</el-button>
        <template v-if="canWrite">
          <el-button :icon="Download" @click="openImport">导入域名</el-button>
          <el-button type="primary" :icon="Plus" @click="openDialog()">新增域名</el-button>
        </template>
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
                v-if="canWrite"
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
                  <el-tooltip
                    v-if="rec.protected" content="受保护记录：改 / 删需走审批" placement="top"
                  >
                    <el-tag size="small" type="warning" class="tag-gap">保护</el-tag>
                  </el-tooltip>
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
              <el-table-column v-if="canWrite" label="操作" width="300">
                <template #default="{ row: rec }">
                  <el-button
                    v-if="rec.sync_status !== 'synced'"
                    size="small"
                    type="warning"
                    :loading="retryingId === rec.id"
                    @click="handleRetry(row, rec)"
                  >重推</el-button>
                  <el-button size="small" :icon="Edit" @click="openRecordDialog(row, rec)">编辑</el-button>
                  <el-button size="small" :icon="Clock" @click="openChanges(rec)">留痕</el-button>
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
      <el-table-column v-if="canWrite || canTopo" label="操作" width="300" fixed="right">
        <template #default="{ row }">
          <el-button v-if="canWrite" size="small" :loading="syncingId === row.id" @click="handleSync(row)">同步</el-button>
          <!-- 拓扑：反查这个域名被画进了哪些接入链路 -->
          <el-button v-if="canTopo" size="small" @click="openImpact(row)">拓扑</el-button>
          <el-button v-if="canWrite" size="small" :icon="Edit" @click="openDialog(row)">编辑</el-button>
          <el-button v-if="canWrite" size="small" type="danger" :icon="Delete" @click="handleDelete(row)">删除</el-button>
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
          >
            <span>{{ s.name }}（{{ s.public_ip || s.private_ip }}）</span>
            <el-tag v-if="s.environment_is_prod" type="danger" size="small" style="margin-left:6px">生产</el-tag>
          </el-option>
        </el-select>
        <div class="tip">记录值自动取主机 IP（公网优先）；指向生产主机属高危变更，需填原因走审批</div>
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
      <el-form-item label="变更原因" prop="reason" :required="formRiskReasons.length > 0">
        <el-input v-model="recordForm.reason" placeholder="如「切流量到新机」，会记入变更留痕" />
        <el-alert
          v-if="formRiskReasons.length" type="warning" :closable="false" show-icon
          style="margin-top:8px"
        >
          本次变更属<b>高危</b>（{{ formRiskReasons.join('；') }}），
          提交后进入<b>审批</b>，需另一位管理员批准才会生效。变更原因必填。
        </el-alert>
        <div v-else-if="editRecord" class="tip">
          修改会即时推送厂商、立即生效；变更前的云端真实值已留痕，可在「变更留痕」中一键回滚
        </div>
        <div v-else class="tip">新增会即时推送厂商、立即生效；原因可留空，建议填写，会记入变更留痕</div>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="recordDialogVisible = false">取消</el-button>
      <el-button type="primary" :loading="recordSubmitting" @click="handleRecordSubmit">确定</el-button>
    </template>
  </el-dialog>

  <!-- DNS 高危变更审批抽屉 -->
  <el-drawer v-model="requestsVisible" title="DNS 高危变更审批" size="70%">
    <el-alert type="warning" :closable="false" show-icon style="margin-bottom:12px">
      只有高危变更需要审批（改 / 删已生效解析、根域 @、NS/MX/CAA/TXT、受保护记录、
      指向生产主机）。<b>不能审批自己提交的变更单</b>；确需自行执行请走紧急通道
      —— 必须填原因，会高亮记入审计。变更单
      {{ approvalTtlHint }} 后失效；执行前还会重新比对线上值，期间被别人改过则拒绝执行。
    </el-alert>
    <div class="toolbar" style="margin-bottom:8px">
      <el-radio-group v-model="requestFilter" @change="loadRequests">
        <el-radio-button value="pending">待审批</el-radio-button>
        <el-radio-button value="">全部</el-radio-button>
      </el-radio-group>
      <el-button :icon="Refresh" @click="loadRequests">刷新</el-button>
    </div>
    <el-table v-loading="requestsLoading" :data="requests" stripe size="small">
      <el-table-column label="时间" width="150">
        <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="变更" min-width="230">
        <template #default="{ row }">
          <el-tag size="small" :type="actionType(row.action)">{{ row.action_label }}</el-tag>
          <span class="fqdn" style="margin-left:6px">{{ row.target_rr }}.{{ row.domain_name }}</span>
          <div class="mono">
            {{ row.before?.value || '（新增）' }} → {{ row.payload?.value || '（删除）' }}
          </div>
        </template>
      </el-table-column>
      <el-table-column label="高危理由" min-width="200">
        <template #default="{ row }">
          <el-tag
            v-for="reason in row.risk_reasons" :key="reason"
            size="small" type="danger" class="tag-gap"
          >{{ reason }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="reason" label="申请原因" min-width="130" show-overflow-tooltip />
      <el-table-column prop="requested_by_name" label="申请人" width="90" />
      <el-table-column label="状态" width="110">
        <template #default="{ row }">
          <el-tag size="small" :type="requestStatusType(row)">
            {{ row.expired ? '已过期' : row.status_label }}
          </el-tag>
          <el-tag v-if="row.emergency" size="small" type="danger" class="tag-gap">紧急自批</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="210" fixed="right">
        <template #default="{ row }">
          <template v-if="row.status === 'pending' && !row.expired">
            <el-button
              v-if="canWrite" size="small" type="success"
              :loading="reviewingId === row.id" @click="handleApprove(row)"
            >批准</el-button>
            <el-button
              v-if="canWrite" size="small" type="danger"
              :loading="reviewingId === row.id" @click="handleReject(row)"
            >驳回</el-button>
            <el-button size="small" @click="handleCancel(row)">撤销</el-button>
          </template>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
    </el-table>
  </el-drawer>

  <!-- DNS 变更留痕抽屉：改/删前的云端真实值 + 一键回滚 -->
  <el-drawer v-model="changesVisible" :title="changesTitle" size="70%">
    <el-alert type="info" :closable="false" show-icon style="margin-bottom:12px">
      改 / 删解析前会先从云厂商拉取当前真实值存档。只有「云厂商实时值」来源的留痕可回滚
      —— 拿本地值去回滚可能把一个从未在云端生效过的值推上线。回滚时云端先动、本地后动，
      云端失败则整体放弃。
    </el-alert>
    <el-table v-loading="changesLoading" :data="changes" stripe size="small">
      <el-table-column label="时间" width="150">
        <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="记录" min-width="170" show-overflow-tooltip>
        <template #default="{ row }">
          <span class="fqdn">{{ row.record_rr }}.{{ row.domain_name }}</span>
          <el-tag size="small" type="info" class="tag-gap">{{ row.record_type }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="动作" width="80">
        <template #default="{ row }">
          <el-tag size="small" :type="actionType(row.action)">{{ row.action_label }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="变更前 → 变更后" min-width="230">
        <template #default="{ row }">
          <span class="mono">{{ row.before?.value || '—' }}</span>
          <span class="muted"> → </span>
          <span class="mono">{{ row.after?.value || '（已删除）' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="前值来源" width="130">
        <template #default="{ row }">
          <el-tag size="small" :type="row.before_source === 'provider' ? 'success' : 'warning'">
            {{ row.before_source_label }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="operator_name" label="操作人" width="90">
        <template #default="{ row }"><span :class="{ muted: !row.operator_name }">{{ row.operator_name || '—' }}</span></template>
      </el-table-column>
      <el-table-column prop="reason" label="原因" min-width="120" show-overflow-tooltip>
        <template #default="{ row }"><span :class="{ muted: !row.reason }">{{ row.reason || '—' }}</span></template>
      </el-table-column>
      <el-table-column v-if="canWrite" label="操作" width="110" fixed="right">
        <template #default="{ row }">
          <el-button
            v-if="row.restorable" size="small" type="warning"
            :loading="restoringId === row.id" @click="handleRestore(row)"
          >回滚</el-button>
          <span v-else-if="row.restored_at" class="muted">已回滚</span>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
    </el-table>
  </el-drawer>

  <!-- 所在拓扑（影响面反查） -->
  <TopoImpactDialog
    v-model="impactVisible"
    kind="domain"
    :ref-id="impactRow.id"
    :label="impactRow.name"
  />
</template>

<script setup>
import { ref, reactive, computed, onMounted, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import { Plus, Edit, Delete, Download, Clock, Finished, Refresh } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useAuthStore } from '../../stores/auth'
import TopoImpactDialog from '../../components/TopoImpactDialog.vue'
import { debounce } from '../../utils/debounce'
import { getServers } from '../../api/assets'
import {
  getCloudAccounts, getDomains, createDomain, updateDomain, deleteDomain,
  importDomains, syncDomainRecords,
  getDnsRecords, createDnsRecord, updateDnsRecord, deleteDnsRecord, retrySyncDnsRecord,
  getDnsChanges, restoreDnsChange,
  getDnsChangeRequests, approveDnsChangeRequest, rejectDnsChangeRequest,
  cancelDnsChangeRequest,
} from '../../api/cloud'

const route = useRoute()
const authStore = useAuthStore()
// 按钮可见性跟角色矩阵走：域名与证书是 cloud 模块，
// 云账号下拉另按 cloud_account 判（只读档也能选账号，AccessKey 是打码的）
const canWrite = computed(() => authStore.can('cloud', 'write'))
// 拓扑入口另按 topo 模块判：改域名解析前先看它挂在哪条接入链路上
const canTopo = computed(() => authStore.can('topo'))

// 影响面反查弹窗
const impactVisible = ref(false)
const impactRow = ref({})

function openImpact(row) {
  impactRow.value = row
  impactVisible.value = true
}
const canAccount = computed(() => authStore.can('cloud_account'))

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

// 搜索：回到第一页再查，避免停留在越界页码上
function handleSearch() {
  searchDebounced.cancel()
  page.value = 1
  fetchData()
}

// 边输边查（300ms 防抖），无需回车
const searchDebounced = debounce(handleSearch)
onUnmounted(() => searchDebounced.cancel())

function clearKeyword() {
  filter.keyword = ''
  handleSearch()
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
  reason: '',  // 变更原因，随改/删请求记入变更留痕
})
const recordForm = reactive(emptyRecordForm())
// 规则用 computed：高危变更的「变更原因」必填，低危时不拦
const recordRules = computed(() => ({
  rr: [{ required: true, message: '请输入主机记录' }],
  type: [{ required: true, message: '请选择类型' }],
  server: [{ required: true, message: '请选择服务器' }],
  value: [{ required: true, message: '请输入记录值' }],
  reason: formRiskReasons.value.length
    ? [{ required: true, message: '高危变更必须填写变更原因，审批人据此判断' }]
    : [],
}))

//: 与后端 dns_risk.HIGH_RISK_TYPES 对齐
const HIGH_RISK_TYPES = ['NS', 'MX', 'CAA', 'TXT']

const selectedServer = computed(
  () => serverOptions.value.find((s) => s.id === recordForm.server) || null,
)

/**
 * 按表单当前值预判高危理由（规则镜像后端 dns_risk.risk_reasons，判定仍以后端为准）。
 *
 * 之前只在编辑弹窗里读后端算好的 record.risk_reasons，于是有两个坑：新增记录命中
 * 高危（比如指向生产主机）时前端根本没有填原因的地方，只能提交后吃一句报错走不下去；
 * 编辑时改了目标（普通机换成生产机）也还是按旧值提示。
 */
const formRiskReasons = computed(() => {
  const reasons = []
  if (editRecord.value?.remote_record_id) reasons.push('修改/删除已生效的线上解析')
  if (recordForm.rr.trim() === '@') reasons.push('根域 @ 记录')
  if (HIGH_RISK_TYPES.includes(recordForm.type)) reasons.push(`${recordForm.type} 记录`)
  if (editRecord.value?.protected) reasons.push('记录被标记为受保护')
  if (recordDomain.value?.protected) reasons.push(`域名 ${recordDomain.value.name} 被标记为受保护`)
  if (recordForm.target_type === 'server' && selectedServer.value?.environment_is_prod) {
    reasons.push('指向生产环境主机')
  }
  return reasons
})

function openRecordDialog(domainRow, rec = null) {
  recordDomain.value = domainRow
  editRecord.value = rec
  Object.assign(recordForm, emptyRecordForm(), rec ? { ...rec, reason: '' } : {})
  recordDialogVisible.value = true
  recordFormRef.value?.clearValidate()
}

async function handleRecordSubmit() {
  await recordFormRef.value.validate()
  recordSubmitting.value = true
  try {
    const payload = { ...recordForm, domain: recordDomain.value.id }
    // 高危变更后端不会直接执行，而是回一张变更单（data.pending_approval）
    if (editRecord.value) {
      const res = await updateDnsRecord(editRecord.value.id, payload)
      _notifySubmit(res, '更新成功')
    } else {
      const res = await createDnsRecord(payload)
      _notifySubmit(res, '新增成功')
    }
    recordDialogVisible.value = false
    await loadRecords(recordDomain.value.id, true)
    fetchData()
  } finally {
    recordSubmitting.value = false
  }
}

function _notifySubmit(res, okMessage) {
  if (res?.data?.pending_approval) {
    ElMessage.warning(res.msg || '该变更属高危操作，已提交审批')
    loadPendingCount()
    return
  }
  ElMessage.success(okMessage)
}

async function handleRecordDelete(domainRow, rec) {
  // 用 prompt 而非 confirm：删除会即时生效在线上，留一句原因是事后排障最省事的线索
  const { value: reason } = await ElMessageBox.prompt(
    `确定删除解析记录「${rec.fqdn} ${rec.type} → ${rec.value}」吗？`
    + '将同步删除厂商侧解析，立即生效。变更前的云端真实值会留痕，可一键回滚。',
    '删除解析记录',
    {
      type: 'warning',
      confirmButtonText: '删除',
      confirmButtonClass: 'el-button--danger',
      inputPlaceholder: rec.remote_record_id ? '变更原因（必填）' : '变更原因（可留空，建议填写）',
      inputValue: '',
      // 删已生效的线上解析是高危，后端强制要原因；在这里就拦住，别让人白点一次
      inputValidator: (v) => (rec.remote_record_id && !(v || '').trim()
        ? '删除已生效的线上解析属高危变更，请填写变更原因'
        : true),
    },
  )
  const res = await deleteDnsRecord(rec.id, reason || '')
  _notifySubmit(res, '删除成功（可在「变更留痕」中回滚）')
  await loadRecords(domainRow.id, true)
  fetchData()
}

// ── DNS 高危变更审批 ──────────────────────────────────
const requestsVisible = ref(false)
const requestsLoading = ref(false)
const requests = ref([])
const requestFilter = ref('pending')
const pendingCount = ref(0)
const reviewingId = ref(null)
// 与后端 DNS_APPROVAL_TTL_HOURS 对齐的提示文案（只是提示，判定在后端）
const approvalTtlHint = '24 小时'

const requestStatusType = (row) => {
  if (row.expired) return 'info'
  return { pending: 'warning', applied: 'success', rejected: 'danger' }[row.status] || 'info'
}

async function openRequests() {
  requestsVisible.value = true
  await loadRequests()
}

async function loadRequests() {
  requestsLoading.value = true
  try {
    const params = { page_size: 100 }
    if (requestFilter.value) params.status = requestFilter.value
    const res = await getDnsChangeRequests(params)
    requests.value = res.data?.results || []
    if (requestFilter.value === 'pending') pendingCount.value = res.data?.total || 0
  } finally {
    requestsLoading.value = false
  }
}

async function loadPendingCount() {
  try {
    const res = await getDnsChangeRequests({ status: 'pending', page_size: 1 })
    pendingCount.value = res.data?.total || 0
  } catch {
    pendingCount.value = 0  // 没权限或接口异常时不显示角标，不影响主流程
  }
}

async function handleApprove(row) {
  // 自批会被后端拒（不能审批自己提交的变更单），这里先问清楚是否走紧急通道
  const { value: comment } = await ElMessageBox.prompt(
    `批准后立即推送厂商并生效：${row.target_rr}.${row.domain_name} → `
    + `${row.payload?.value || '（删除）'}。`
    + '若这是你自己提交的变更单，需填写原因走紧急通道（会高亮记入审计）。',
    '批准变更',
    { confirmButtonText: '批准并执行', inputPlaceholder: '审批意见 / 紧急原因（自批必填）' },
  )
  reviewingId.value = row.id
  try {
    const isSelf = row.requested_by_name === authStore.user?.username
    const res = await approveDnsChangeRequest(row.id, {
      comment: comment || '', emergency: isSelf && !!comment,
    })
    ElMessage.success(res.msg || '已执行')
    await loadRequests()
    fetchData()
  } finally {
    reviewingId.value = null
  }
}

async function handleReject(row) {
  const { value: comment } = await ElMessageBox.prompt(
    `驳回「${row.action_label} ${row.target_rr}.${row.domain_name}」？`,
    '驳回变更',
    { type: 'warning', confirmButtonText: '驳回', inputPlaceholder: '驳回理由' },
  )
  reviewingId.value = row.id
  try {
    await rejectDnsChangeRequest(row.id, comment || '')
    ElMessage.success('已驳回')
    await loadRequests()
  } finally {
    reviewingId.value = null
  }
}

async function handleCancel(row) {
  await ElMessageBox.confirm('确定撤销该变更单吗？', '撤销', { type: 'warning' })
  await cancelDnsChangeRequest(row.id)
  ElMessage.success('已撤销')
  await loadRequests()
}

// ── DNS 变更留痕 ──────────────────────────────────────
const changesVisible = ref(false)
const changesLoading = ref(false)
const changes = ref([])
const changesTitle = ref('DNS 变更留痕')
const changesFilter = ref({})
const restoringId = ref(null)

const actionType = (action) => ({
  create: 'success', update: 'warning', delete: 'danger', restore: 'info',
}[action] || 'info')

async function openChanges(rec = null) {
  changesFilter.value = rec ? { record: rec.id } : {}
  changesTitle.value = rec
    ? `变更留痕 — ${rec.fqdn} ${rec.type}`
    : 'DNS 变更留痕（全部域名）'
  changesVisible.value = true
  await loadChanges()
}

async function loadChanges() {
  changesLoading.value = true
  try {
    const res = await getDnsChanges({ ...changesFilter.value, page_size: 100 })
    changes.value = res.data?.results || []
  } finally {
    changesLoading.value = false
  }
}

async function handleRestore(row) {
  const target = row.before?.value ?? '（重建记录）'
  await ElMessageBox.confirm(
    `确定回滚这次${row.action_label}吗？`
    + `「${row.record_rr}.${row.domain_name}」将恢复为 ${target}，即时推送厂商并立即生效。`,
    '回滚 DNS 变更',
    { type: 'warning', confirmButtonText: '回滚' },
  )
  restoringId.value = row.id
  try {
    const res = await restoreDnsChange(row.id)
    ElMessage.success(res.msg || '已回滚')
    await loadChanges()
    fetchData()
  } finally {
    restoringId.value = null
  }
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
  // 概览页的待办点进来：?requests=pending 直接开审批抽屉（否则落到列表页还要
  // 自己找「变更审批」按钮）；?keyword= 预置搜索定位到具体域名
  const q = route.query
  if (q.keyword) filter.keyword = String(q.keyword)
  fetchData()
  if (q.requests && canWrite.value) openRequests()
  if (canWrite.value) loadPendingCount()  // 「变更审批」角标：有待审的单子要能一眼看见
  // 云账号下拉（域名归属/导入用）；服务器下拉（记录绑定用，page_size=0 全量）
  if (canWrite.value) {
    // 云账号与服务器分属另外两个模块，逐个按权限取：Promise.all 一拒全拒
    const [accRes, svrRes] = await Promise.all([
      canAccount.value ? getCloudAccounts({ page_size: 0 }) : null,
      authStore.can('cmdb') ? getServers({ page_size: 0 }) : null,
    ])
    accountOptions.value = accRes?.data?.results || []
    serverOptions.value = svrRes?.data?.results || []
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
.tag-gap { margin-left: 6px; }
.mono { font-family: Consolas, Monaco, monospace; font-size: 12px; }
</style>
