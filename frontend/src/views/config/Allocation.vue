<template>
  <!-- 分配概览卡片：点击未分配卡片快速筛选 -->
  <div class="overview-row">
    <el-card
      v-for="card in overviewCards"
      :key="card.key"
      shadow="never"
      class="overview-card"
      :class="{ active: card.filterValue !== null && filter.unassigned === card.filterValue }"
      @click="handleCardClick(card)"
    >
      <div class="card-label">{{ card.label }}</div>
      <div class="card-value" :class="{ warn: card.filterValue && card.value > 0 }">{{ card.value }}</div>
    </el-card>
  </div>

  <el-card shadow="never">
    <!-- 工具栏 -->
    <div class="toolbar">
      <div class="toolbar-left">
        <!-- 本页仅展示已规划服务的主机，「未规划服务」筛选不再适用（未规划主机走「分配服务」表单纳管） -->
        <el-select v-model="filter.unassigned" placeholder="未分配维度" clearable style="width:140px" @change="fetchData">
          <el-option label="任一维度未分配" value="any" />
          <el-option label="未分配项目组" value="project_group" />
          <el-option label="未分配环境" value="environment" />
          <el-option label="未分配渠道" value="channel" />
          <el-option label="未分配云厂商" value="cloud_provider" />
        </el-select>
        <el-select v-model="filter.project_group" placeholder="项目组" clearable style="width:130px" @change="fetchData">
          <el-option v-for="o in dicts.projectGroups" :key="o.id" :label="o.name" :value="o.code" />
        </el-select>
        <el-select v-model="filter.env" placeholder="环境" clearable style="width:110px" @change="fetchData">
          <el-option v-for="o in dicts.environments" :key="o.id" :label="o.name" :value="o.code" />
        </el-select>
        <el-select v-model="filter.channel" placeholder="渠道" clearable style="width:120px" @change="fetchData">
          <el-option v-for="o in dicts.channels" :key="o.id" :label="o.name" :value="o.code" />
        </el-select>
        <el-select v-model="filter.service_type" placeholder="服务类型" clearable style="width:130px" @change="fetchData">
          <el-option v-for="o in dicts.serviceTypes" :key="o.id" :label="o.name" :value="o.code" />
        </el-select>
        <el-select v-model="filter.service_status" placeholder="服务状态" clearable style="width:110px" @change="fetchData">
          <el-option v-for="(label, key) in SERVICE_STATUS_LABELS" :key="key" :label="label" :value="key" />
        </el-select>
        <el-input
          v-model="filter.keyword"
          placeholder="名称/IP"
          clearable
          style="width:160px"
          @keyup.enter="fetchData"
          @clear="fetchData"
        />
      </div>
      <!-- 批量分配/规划均为修改操作，仅管理员可见 -->
      <div v-if="authStore.isStaff" class="toolbar-right">
        <el-button type="success" :icon="Plus" @click="openAllocateDialog">
          分配服务<template v-if="overview.unassigned.service_type">（{{ overview.unassigned.service_type }} 台未规划）</template>
        </el-button>
        <el-button
          :icon="Menu"
          :disabled="!selection.length"
          @click="openPlanDialog"
        >
          规划服务<template v-if="selection.length">（{{ selection.length }} 台）</template>
        </el-button>
        <el-button
          type="primary"
          :icon="Connection"
          :disabled="!selection.length"
          @click="openAssignDialog"
        >
          批量分配<template v-if="selection.length">（{{ selection.length }} 台）</template>
        </el-button>
      </div>
    </div>

    <!-- 服务器表格（支持跨页多选） -->
    <el-table
      v-loading="loading"
      :data="tableData"
      row-key="id"
      stripe
      @selection-change="(rows) => (selection = rows)"
    >
      <el-table-column v-if="authStore.isStaff" type="selection" width="46" reserve-selection />
      <el-table-column prop="name" label="名称" min-width="120" />
      <el-table-column prop="ip" label="IP 地址" width="130" />
      <el-table-column label="项目组" min-width="110">
        <template #default="{ row }">
          <span :class="{ unset: !row.project_group_name }">{{ row.project_group_name || '未分配' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="环境" width="100">
        <template #default="{ row }">
          <el-tag v-if="row.environment_name" size="small" :type="row.environment_is_prod ? 'danger' : 'info'">
            {{ row.environment_name }}
          </el-tag>
          <span v-else class="unset">未分配</span>
        </template>
      </el-table-column>
      <el-table-column label="渠道" min-width="100">
        <template #default="{ row }">
          <span :class="{ unset: !row.channel_name }">{{ row.channel_name || '未分配' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="云厂商/地域" min-width="130">
        <template #default="{ row }">
          <span :class="{ unset: !row.cloud_provider_name }">
            {{ row.cloud_provider_name || '未分配' }}</span><template v-if="row.region"> / {{ row.region }}</template>
        </template>
      </el-table-column>
      <el-table-column label="服务进程（规划/部署）" min-width="200">
        <template #default="{ row }">
          <template v-if="row.services?.length">
            <!-- 进程名与进程数同层级展示（如 nginx-01 ×2），方便直观确认进程数量 -->
            <el-tag
              v-for="svc in row.services"
              :key="svc.id"
              size="small"
              :type="SERVICE_STATUS_TAG[svc.status]"
              class="svc-tag"
            >
              {{ svc.process_name || svc.service_type_name }} ×{{ svc.process_count }} · {{ SERVICE_STATUS_LABELS[svc.status] }}
            </el-tag>
          </template>
          <span v-else class="unset">未规划</span>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tag size="small" :type="statusType(row.status)">{{ statusLabel(row.status) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column v-if="authStore.isStaff" label="操作" width="90" fixed="right">
        <template #default="{ row }">
          <el-button size="small" link type="primary" @click="openServiceDialog(row)">服务管理</el-button>
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

  <!-- 批量分配弹窗 -->
  <el-dialog v-model="dialogVisible" :title="`批量分配（已选 ${selection.length} 台服务器）`" width="480px">
    <el-alert type="info" :closable="false" show-icon style="margin-bottom:16px">
      各维度已回显当前值（多台不一致时显示「多个值」），仅改动了的维度会被提交
    </el-alert>
    <el-form label-width="90px">
      <el-form-item v-for="dim in assignDims" :key="dim.key" :label="dim.label">
        <el-select v-model="assignForm[dim.key]" style="width:100%">
          <!-- 「多个值」「未分配」为当前值回显项，保持选中即不修改 -->
          <el-option v-if="baseline[dim.key] === MIXED" label="多个值（保持不变）" :value="MIXED" />
          <el-option v-if="baseline[dim.key] === NONE" label="未分配（当前）" :value="NONE" />
          <el-option v-else label="清空" :value="CLEAR" />
          <el-option
            v-for="o in [...extraOptions[dim.key], ...dim.options()]"
            :key="o.id"
            :label="o.name"
            :value="o.id"
          />
        </el-select>
      </el-form-item>
      <el-form-item label="地域/机房">
        <div class="region-line">
          <el-checkbox v-model="assignForm.changeRegion">修改</el-checkbox>
          <el-input
            v-model="assignForm.region"
            :disabled="!assignForm.changeRegion"
            :placeholder="baseline.region === MIXED ? '多个值，勾选修改后统一设置（留空表示清空）' : '如 cn-hangzhou，留空表示清空'"
          />
        </div>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="dialogVisible = false">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="handleAssign">确定</el-button>
    </template>
  </el-dialog>

  <!-- 批量规划服务弹窗（对表格中已选主机追加服务） -->
  <el-dialog v-model="planDialogVisible" :title="`规划服务（已选 ${selection.length} 台服务器）`" width="480px">
    <el-alert type="info" :closable="false" show-icon style="margin-bottom:16px">
      为选中主机创建「已规划」服务记录并自动生成进程名（如 nginx-01）；已存在的记录自动跳过
    </el-alert>
    <el-form label-width="90px">
      <el-form-item label="服务类型">
        <el-select v-model="planForm.serviceTypeIds" multiple style="width:100%" placeholder="选择要规划的服务类型">
          <el-option v-for="o in dicts.serviceTypes" :key="o.id" :label="o.name" :value="o.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="进程数">
        <el-input-number v-model="planForm.processCount" :min="1" :max="99" style="width:160px" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="planDialogVisible = false">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="handlePlan">确定</el-button>
    </template>
  </el-dialog>

  <!-- 分配服务弹窗：把未规划服务的主机纳入分配（本页表格只显示已规划主机） -->
  <el-dialog v-model="allocateDialogVisible" title="分配服务" width="560px">
    <el-alert type="info" :closable="false" show-icon style="margin-bottom:16px">
      本页仅显示已规划服务的主机；在此为「未规划服务」的主机分配服务类型后，主机将出现在列表中并获得任务执行权限
    </el-alert>
    <el-form label-width="90px">
      <!-- 主机筛选：按项目组/渠道/环境按需拉取候选主机；切换筛选不清空已选（支持跨项目/渠道/环境累计勾选） -->
      <el-form-item label="主机筛选">
        <div class="allocate-filter">
          <el-select v-model="allocateFilter.project_group" placeholder="项目组" clearable style="flex:1" @change="loadUnplannedServers">
            <el-option v-for="o in dicts.projectGroups" :key="o.id" :label="o.name" :value="o.code" />
          </el-select>
          <el-select v-model="allocateFilter.channel" placeholder="渠道" clearable style="flex:1" @change="loadUnplannedServers">
            <el-option v-for="o in dicts.channels" :key="o.id" :label="o.name" :value="o.code" />
          </el-select>
          <el-select v-model="allocateFilter.env" placeholder="环境" clearable style="flex:1" @change="loadUnplannedServers">
            <el-option v-for="o in dicts.environments" :key="o.id" :label="o.name" :value="o.code" />
          </el-select>
        </div>
      </el-form-item>
      <el-form-item label="目标主机">
        <el-select
          v-model="allocateForm.serverIds"
          multiple
          filterable
          style="width:100%"
          :loading="allocateLoading"
          placeholder="选择未规划服务的主机"
        >
          <el-option v-for="s in allocateOptions" :key="s.id" :label="serverOptionLabel(s)" :value="s.id" />
        </el-select>
        <div v-if="!allocateLoading && !unplannedServers.length" class="form-tip">
          当前筛选条件下没有未规划服务的主机{{ allocateForm.serverIds.length ? '（已选主机保留在上方）' : '' }}
        </div>
      </el-form-item>
      <el-form-item label="服务类型">
        <el-select v-model="allocateForm.serviceTypeIds" multiple style="width:100%" placeholder="选择要规划的服务类型">
          <el-option v-for="o in dicts.serviceTypes" :key="o.id" :label="o.name" :value="o.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="进程数">
        <el-input-number v-model="allocateForm.processCount" :min="1" :max="99" style="width:160px" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="allocateDialogVisible = false">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="handleAllocate">确定</el-button>
    </template>
  </el-dialog>

  <!-- 单机服务管理弹窗：状态流转 / 取消规划 / 补充规划 -->
  <el-dialog v-model="serviceDialogVisible" :title="`服务管理 - ${serviceRow?.name || ''}`" width="680px">
    <el-table :data="serviceRow?.services || []" size="small">
      <el-table-column prop="service_type_name" label="服务类型" min-width="100" />
      <el-table-column label="进程名" min-width="110">
        <template #default="{ row }">
          <span :class="{ unset: !row.process_name }">{{ row.process_name || '—' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="进程数" width="130">
        <template #default="{ row }">
          <!-- 直接改数字即保存；min 锁定当前值：进程数只允许调大（缩容走下线流程），进程名保持稳定 -->
          <el-input-number
            :model-value="row.process_count"
            :min="row.process_count"
            :max="99"
            size="small"
            style="width:100px"
            @change="(v) => handleProcessCount(row, v)"
          />
        </template>
      </el-table-column>
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tag size="small" :type="SERVICE_STATUS_TAG[row.status]">{{ SERVICE_STATUS_LABELS[row.status] }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" min-width="220">
        <template #default="{ row }">
          <!-- 按当前状态展示允许的流转动作（与后端 STATUS_TRANSITIONS 保持一致） -->
          <el-button v-if="row.status === 'planned'" size="small" type="success" link @click="handleMark(row, 'deployed')">标记已部署</el-button>
          <el-button v-if="row.status === 'planned'" size="small" type="danger" link @click="handleUnplan(row)">取消规划</el-button>
          <el-button v-if="row.status === 'deployed'" size="small" type="warning" link @click="handleMark(row, 'offline')">标记下线</el-button>
          <el-button v-if="row.status === 'deployed'" size="small" link @click="handleMark(row, 'planned')">回退为已规划</el-button>
          <el-button v-if="row.status === 'offline'" size="small" link @click="handleMark(row, 'planned')">重新规划</el-button>
        </template>
      </el-table-column>
    </el-table>
    <template #footer>
      <el-button @click="serviceDialogVisible = false">关闭</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { Connection, Menu, Plus } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  getServers, projectGroupApi, channelApi, environmentApi, cloudProviderApi, serviceTypeApi, fetchDictOptions,
} from '../../api/assets'
import {
  batchAssign, getAllocationOverview, planServices, markServices, unplanServices, updateServiceProcess,
} from '../../api/config'
import { useAuthStore } from '../../stores/auth'

const authStore = useAuthStore()

// 下拉哨兵值：清空 / 多台不一致 / 当前未分配（后两者为当前值回显，保持选中即不修改）
const CLEAR = '__CLEAR__'
const MIXED = '__MIXED__'
const NONE = '__NONE__'

// 主机服务三态：已规划（可部署）→ 已部署 → 已下线
const SERVICE_STATUS_LABELS = { planned: '已规划', deployed: '已部署', offline: '已下线' }
const SERVICE_STATUS_TAG = { planned: 'warning', deployed: 'success', offline: 'info' }

const loading = ref(false)
const submitting = ref(false)
const tableData = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const selection = ref([])
const dialogVisible = ref(false)
const planDialogVisible = ref(false)
const allocateDialogVisible = ref(false)
const allocateLoading = ref(false)
// 分配服务弹窗的候选主机：当前筛选条件下未规划任何服务的主机
const unplannedServers = ref([])
// 弹窗内的主机筛选条件（项目组/渠道/环境，按 code 传参）
const allocateFilter = reactive({ project_group: '', channel: '', env: '' })
// 已加载过的主机缓存（id → 主机对象）：切换筛选后已选主机仍能正确显示名称
const allocateSeen = new Map()
const serviceDialogVisible = ref(false)
// 服务管理弹窗当前操作的服务器行
const serviceRow = ref(null)
const filter = reactive({
  unassigned: '', project_group: '', env: '', channel: '',
  service_type: '', service_status: '', keyword: '',
})
const overview = ref({ total: 0, unassigned: {} })

// CMDB 字典下拉选项（仅启用项）
const dicts = reactive({
  environments: [], projectGroups: [], channels: [], cloudProviders: [], serviceTypes: [],
})

// 批量分配维度定义
const assignDims = [
  { key: 'project_group', label: '项目组', options: () => dicts.projectGroups },
  { key: 'environment', label: '环境', options: () => dicts.environments },
  { key: 'channel', label: '渠道', options: () => dicts.channels },
  { key: 'cloud_provider', label: '云厂商', options: () => dicts.cloudProviders },
]
const assignForm = reactive({
  project_group: NONE, environment: NONE, channel: NONE, cloud_provider: NONE,
  changeRegion: false, region: '',
})
// 打开弹窗时计算的各维度当前值基线：提交时与之比对，未变化的维度不提交
const baseline = reactive({
  project_group: NONE, environment: NONE, channel: NONE, cloud_provider: NONE, region: '',
})
// 当前值指向已停用字典项时的补充展示选项（不在启用项下拉里）
const extraOptions = reactive({
  project_group: [], environment: [], channel: [], cloud_provider: [],
})
const planForm = reactive({ serviceTypeIds: [], processCount: 1 })
const allocateForm = reactive({ serverIds: [], serviceTypeIds: [], processCount: 1 })

// filterValue 为 null 的卡片不参与未分配筛选（未规划主机不在本页列表，点击改为打开分配服务弹窗）
const overviewCards = computed(() => [
  { key: 'total', label: '已规划服务器', value: overview.value.total - (overview.value.unassigned.service_type ?? 0), filterValue: '' },
  { key: 'pg', label: '未分配项目组', value: overview.value.unassigned.project_group ?? 0, filterValue: 'project_group' },
  { key: 'env', label: '未分配环境', value: overview.value.unassigned.environment ?? 0, filterValue: 'environment' },
  { key: 'ch', label: '未分配渠道', value: overview.value.unassigned.channel ?? 0, filterValue: 'channel' },
  { key: 'svc', label: '未规划服务', value: overview.value.unassigned.service_type ?? 0, filterValue: null },
])

const statusType = (s) => ({ running: 'success', stopped: 'danger', maintenance: 'warning' }[s] || 'info')
const statusLabel = (s) => ({ running: '运行中', stopped: '已停止', maintenance: '维护中' }[s] || s)

async function loadDicts() {
  const [envs, groups, channels, providers, serviceTypes] = await Promise.all([
    fetchDictOptions(environmentApi),
    fetchDictOptions(projectGroupApi),
    fetchDictOptions(channelApi),
    fetchDictOptions(cloudProviderApi),
    fetchDictOptions(serviceTypeApi),
  ])
  dicts.environments = envs.data?.results || []
  dicts.projectGroups = groups.data?.results || []
  dicts.channels = channels.data?.results || []
  dicts.cloudProviders = providers.data?.results || []
  dicts.serviceTypes = serviceTypes.data?.results || []
}

async function loadOverview() {
  const res = await getAllocationOverview()
  overview.value = res.data || { total: 0, unassigned: {} }
}

async function fetchData() {
  loading.value = true
  try {
    // assigned=service_type：本页只显示已规划服务的主机，未规划主机通过「分配服务」纳管
    const params = { page: page.value, page_size: pageSize.value, assigned: 'service_type' }
    Object.entries(filter).forEach(([key, value]) => {
      if (value) params[key] = value
    })
    const res = await getServers(params)
    tableData.value = res.data?.results || []
    total.value = res.data?.total || 0
  } finally {
    loading.value = false
  }
}

// 点击概览卡片：未分配类卡片切换筛选（再次点击取消）；「未规划服务」卡片打开分配服务弹窗
function handleCardClick(card) {
  if (card.filterValue === null) {
    if (authStore.isStaff) openAllocateDialog()
    return
  }
  filter.unassigned = filter.unassigned === card.filterValue ? '' : card.filterValue
  page.value = 1
  fetchData()
}

function openAssignDialog() {
  // 回显选中主机各维度的当前值：一致 → 直接显示该值；不一致 → 「多个值」；全部为空 → 「未分配」
  assignDims.forEach((dim) => {
    const key = dim.key
    const values = new Set(selection.value.map((r) => r[key] ?? NONE))
    const base = values.size > 1 ? MIXED : [...values][0]
    baseline[key] = base
    assignForm[key] = base
    // 当前值为已停用字典项时补一个展示选项（保持选中不会被提交）
    extraOptions[key] = []
    if (typeof base === 'number' && !dim.options().some((o) => o.id === base)) {
      const row = selection.value.find((r) => r[key] === base)
      extraOptions[key] = [{ id: base, name: `${row?.[`${key}_name`] || `#${base}`}（已停用）` }]
    }
  })
  const regions = new Set(selection.value.map((r) => r.region || ''))
  baseline.region = regions.size > 1 ? MIXED : [...regions][0]
  assignForm.region = regions.size > 1 ? '' : baseline.region
  assignForm.changeRegion = false
  dialogVisible.value = true
}

async function handleAssign() {
  // 组装 assign：与当前值一致（含「多个值」「未分配」回显项）的维度不提交，清空提交 null
  const assign = {}
  assignDims.forEach(({ key }) => {
    const value = assignForm[key]
    if (value === baseline[key] || value === MIXED || value === NONE) return
    assign[key] = value === CLEAR ? null : value
  })
  if (assignForm.changeRegion && (baseline.region === MIXED || assignForm.region !== baseline.region)) {
    assign.region = assignForm.region
  }
  if (!Object.keys(assign).length) {
    ElMessage.warning('所有维度均与当前值一致，无需提交')
    return
  }
  submitting.value = true
  try {
    const res = await batchAssign({ server_ids: selection.value.map((r) => r.id), assign })
    ElMessage.success(`已更新 ${res.data?.updated ?? 0} 台服务器`)
    dialogVisible.value = false
    selection.value = []
    fetchData()
    loadOverview()
  } finally {
    submitting.value = false
  }
}

// ---------- 服务规划 ----------
function openPlanDialog() {
  planForm.serviceTypeIds = []
  planForm.processCount = 1
  planDialogVisible.value = true
}

async function handlePlan() {
  if (!planForm.serviceTypeIds.length) {
    ElMessage.warning('请至少选择一个服务类型')
    return
  }
  submitting.value = true
  try {
    const res = await planServices({
      server_ids: selection.value.map((r) => r.id),
      service_type_ids: planForm.serviceTypeIds,
      process_count: planForm.processCount,
    })
    ElMessage.success(`已规划 ${res.data?.created ?? 0} 条服务记录（跳过已存在 ${res.data?.skipped ?? 0} 条）`)
    planDialogVisible.value = false
    selection.value = []
    refreshAll()
  } finally {
    submitting.value = false
  }
}

// ---------- 分配服务（未规划主机纳管） ----------
// 候选项 = 当前筛选结果 + 已选但不在当前筛选内的主机（从缓存取，保证跨筛选累计勾选时标签正常显示）
const allocateOptions = computed(() => {
  const current = unplannedServers.value
  const currentIds = new Set(current.map((s) => s.id))
  const kept = allocateForm.serverIds
    .filter((id) => !currentIds.has(id) && allocateSeen.has(id))
    .map((id) => allocateSeen.get(id))
  return [...kept, ...current]
})

// 已选但被筛掉的主机在标签中标注归属，便于确认跨项目/环境勾选无误
function serverOptionLabel(s) {
  const dims = [s.project_group_name, s.environment_name].filter(Boolean).join('/')
  return `${s.name} (${s.ip})${dims ? ` - ${dims}` : ''}`
}

async function loadUnplannedServers() {
  allocateLoading.value = true
  try {
    const params = { unassigned: 'service_type', page_size: 200 }
    Object.entries(allocateFilter).forEach(([key, value]) => {
      if (value) params[key] = value
    })
    const res = await getServers(params)
    unplannedServers.value = res.data?.results || []
    unplannedServers.value.forEach((s) => allocateSeen.set(s.id, s))
  } finally {
    allocateLoading.value = false
  }
}

async function openAllocateDialog() {
  allocateForm.serverIds = []
  allocateForm.serviceTypeIds = []
  allocateForm.processCount = 1
  allocateFilter.project_group = ''
  allocateFilter.channel = ''
  allocateFilter.env = ''
  allocateSeen.clear()
  allocateDialogVisible.value = true
  loadUnplannedServers()
}

async function handleAllocate() {
  if (!allocateForm.serverIds.length || !allocateForm.serviceTypeIds.length) {
    ElMessage.warning('请选择目标主机和服务类型')
    return
  }
  submitting.value = true
  try {
    const res = await planServices({
      server_ids: allocateForm.serverIds,
      service_type_ids: allocateForm.serviceTypeIds,
      process_count: allocateForm.processCount,
    })
    ElMessage.success(`已为 ${allocateForm.serverIds.length} 台主机分配 ${res.data?.created ?? 0} 条服务`)
    allocateDialogVisible.value = false
    refreshAll()
  } finally {
    submitting.value = false
  }
}

function openServiceDialog(row) {
  serviceRow.value = row
  serviceDialogVisible.value = true
}

// 操作后刷新列表与概览，并同步服务管理弹窗中的行数据
async function refreshAll() {
  await Promise.all([fetchData(), loadOverview()])
  if (serviceRow.value) {
    serviceRow.value = tableData.value.find((r) => r.id === serviceRow.value.id) || null
    if (!serviceRow.value) serviceDialogVisible.value = false
  }
}

async function handleMark(svc, status) {
  // 下线属于敏感操作，二次确认
  if (status === 'offline') {
    await ElMessageBox.confirm(
      `确定将「${svc.service_type_name}」标记为已下线吗？记录将保留用于追溯。`,
      '警告',
      { type: 'warning' },
    )
  }
  const res = await markServices({
    server_ids: [serviceRow.value.id],
    service_type_id: svc.service_type,
    status,
  })
  ElMessage.success(`已更新 ${res.data?.updated ?? 0} 条记录`)
  refreshAll()
}

// 修改进程数：只允许调大（min 已锁定当前值，后端同样校验）
async function handleProcessCount(svc, value) {
  if (!value || value === svc.process_count) return
  const res = await updateServiceProcess({
    server_ids: [serviceRow.value.id],
    service_type_id: svc.service_type,
    process_count: value,
  })
  ElMessage.success(`已更新 ${res.data?.updated ?? 0} 条记录的进程数`)
  refreshAll()
}

async function handleUnplan(svc) {
  await ElMessageBox.confirm(
    `确定取消「${svc.service_type_name}」的规划吗？`,
    '提示',
    { type: 'warning' },
  )
  const res = await unplanServices({
    server_ids: [serviceRow.value.id],
    service_type_id: svc.service_type,
  })
  ElMessage.success(`已取消 ${res.data?.deleted ?? 0} 条规划`)
  refreshAll()
}

onMounted(() => {
  loadDicts()
  loadOverview()
  fetchData()
})
</script>

<style scoped>
.overview-row { display: flex; gap: 16px; margin-bottom: 16px; }
.overview-card { flex: 1; cursor: pointer; transition: border-color 0.2s; }
.overview-card.active { border-color: var(--el-color-primary); }
.card-label { font-size: 13px; color: #909399; }
.card-value { font-size: 26px; font-weight: 600; margin-top: 4px; }
.card-value.warn { color: #e6a23c; }
.toolbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.toolbar-left { display: flex; gap: 8px; flex-wrap: wrap; }
.toolbar-right { display: flex; gap: 8px; flex-shrink: 0; }
.svc-tag { margin: 2px 4px 2px 0; }
.unset { color: #c0c4cc; }
.region-line { display: flex; gap: 8px; width: 100%; }
.form-tip { color: #909399; font-size: 12px; line-height: 1.5; margin-top: 4px; }
.allocate-filter { display: flex; gap: 8px; width: 100%; }
</style>
