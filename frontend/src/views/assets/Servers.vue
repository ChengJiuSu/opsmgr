<template>
  <el-card shadow="never">
    <!-- 工具栏 -->
    <div class="toolbar">
      <div class="toolbar-left">
        <el-input
          v-model="filter.keyword"
          placeholder="搜索名称 / IP"
          clearable
          style="width:180px"
          @input="searchDebounced"
          @keyup.enter="handleSearch"
          @clear="handleSearch"
        />
        <el-select v-model="filter.status" placeholder="状态" clearable style="width:110px" @change="handleSearch">
          <el-option label="运行中" value="running" />
          <el-option label="已停止" value="stopped" />
          <el-option label="维护中" value="maintenance" />
        </el-select>
        <el-select v-model="filter.env" placeholder="环境" clearable style="width:110px" @change="handleSearch">
          <el-option v-for="o in dicts.environments" :key="o.id" :label="o.name" :value="o.code" />
        </el-select>
        <el-select v-model="filter.project_group" placeholder="项目组" clearable style="width:130px" @change="handleSearch">
          <el-option v-for="o in dicts.projectGroups" :key="o.id" :label="o.name" :value="o.code" />
        </el-select>
        <el-select v-model="filter.channel" placeholder="渠道" clearable style="width:120px" @change="handleSearch">
          <el-option v-for="o in dicts.channels" :key="o.id" :label="o.name" :value="o.code" />
        </el-select>
        <el-select v-model="filter.cloud_provider" placeholder="云厂商" clearable style="width:120px" @change="handleSearch">
          <el-option v-for="o in dicts.cloudProviders" :key="o.id" :label="o.name" :value="o.code" />
        </el-select>
      </div>
      <div class="toolbar-right">
        <template v-if="canWrite">
          <el-button :icon="EditPen" :disabled="!selection.length" @click="openBatchEdit">
            批量编辑<template v-if="selection.length">（{{ selection.length }}）</template>
          </el-button>
          <el-button :icon="Connection" :disabled="!selection.length" @click="handleRegisterJumpServer">
            注册 JumpServer<template v-if="selection.length">（{{ selection.length }}）</template>
          </el-button>
          <el-button type="danger" :icon="Delete" :disabled="!selection.length" @click="handleBatchDelete">
            批量删除<template v-if="selection.length">（{{ selection.length }}）</template>
          </el-button>
          <el-divider direction="vertical" />
          <el-button :icon="Upload" @click="openImport">导入</el-button>
        </template>
        <el-button :icon="Download" @click="handleExport">导出</el-button>
        <el-button v-if="canCreate" type="primary" :icon="Plus" @click="openDialog()">新增服务器</el-button>
      </div>
    </div>

    <!-- 表格（支持跨页多选做批量操作） -->
    <el-table
      v-loading="loading"
      :data="tableData"
      row-key="id"
      stripe
      @selection-change="(rows) => (selection = rows)"
    >
      <el-table-column v-if="canWrite" type="selection" width="46" reserve-selection />
      <el-table-column prop="name" label="名称" min-width="120" />
      <el-table-column prop="private_ip" label="私网 IP" width="130" />
      <el-table-column label="公网 IP" width="130">
        <template #default="{ row }">{{ row.public_ip || '-' }}</template>
      </el-table-column>
      <el-table-column label="配置" width="110">
        <template #default="{ row }">
          {{ row.cpu }}核 / {{ (row.memory / 1024).toFixed(0) }}GB
        </template>
      </el-table-column>
      <el-table-column label="环境" width="90">
        <template #default="{ row }">
          <el-tag v-if="row.environment_name" size="small" :type="row.environment_is_prod ? 'danger' : 'info'">
            {{ row.environment_name }}
          </el-tag>
          <span v-else>-</span>
        </template>
      </el-table-column>
      <el-table-column label="项目组" min-width="110">
        <template #default="{ row }">{{ row.project_group_name || '-' }}</template>
      </el-table-column>
      <el-table-column label="渠道" min-width="90">
        <template #default="{ row }">{{ row.channel_name || '-' }}</template>
      </el-table-column>
      <el-table-column label="云厂商/地域" min-width="130">
        <template #default="{ row }">
          {{ row.cloud_provider_name || '-' }}<template v-if="row.region"> / {{ row.region }}</template>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tag size="small" :type="statusType(row.status)">{{ statusLabel(row.status) }}</el-tag>
        </template>
      </el-table-column>
      <!-- 展示解析后的实际通道：来源可能是主机指定，也可能是项目组/环境路由规则 -->
      <el-table-column label="执行通道" width="150">
        <template #default="{ row }">
          <template v-if="row.effective_agent_name">
            <el-tag size="small" type="warning">{{ row.effective_agent_name }}</el-tag>
            <span class="channel-source">{{ row.channel_source === 'host' ? '主机' : '规则' }}</span>
          </template>
          <template v-else>
            <span class="muted">直连</span>
            <span v-if="row.channel_mode === 'direct'" class="channel-source">强制</span>
          </template>
        </template>
      </el-table-column>
      <el-table-column v-if="canWrite || canTopo" label="操作" width="210" fixed="right">
        <template #default="{ row }">
          <!-- 拓扑：反查这台主机被画进了哪些接入链路，下线前先看上游还有没有流量 -->
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

  <!-- 新增/编辑弹窗 -->
  <el-dialog v-model="dialogVisible" :title="editRow ? '编辑服务器' : '新增服务器'" width="560px">
    <el-form ref="formRef" :model="form" :rules="rules" label-width="90px">
      <el-row :gutter="16">
        <el-col :span="12">
          <el-form-item label="名称" prop="name">
            <el-input v-model="form.name" />
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item label="私网 IP" prop="private_ip">
            <el-input v-model="form.private_ip" />
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item label="公网 IP" prop="public_ip">
            <el-input v-model="form.public_ip" placeholder="NAT 出局主机留空" />
          </el-form-item>
        </el-col>
        <!-- 操作系统名称较长（如 Windows Server 2019），独占整行；CPU/内存用标准半行，避免下拉框过窄看不见选中值 -->
        <el-col :span="24">
          <el-form-item label="操作系统">
            <el-select v-model="form.os" filterable allow-create default-first-option clearable placeholder="选择或输入" style="width:100%">
              <el-option v-for="o in OS_OPTIONS" :key="o" :label="o" :value="o" />
            </el-select>
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item label="CPU(核)">
            <el-select v-model="form.cpu" clearable placeholder="选择" style="width:100%">
              <el-option v-for="n in CPU_OPTIONS" :key="n" :label="`${n} 核`" :value="n" />
            </el-select>
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item label="内存">
            <el-select v-model="form.memory" clearable placeholder="选择" style="width:100%">
              <el-option v-for="m in MEMORY_OPTIONS" :key="m" :label="`${m / 1024} GB`" :value="m" />
            </el-select>
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item label="项目组">
            <el-select v-model="form.project_group" clearable style="width:100%">
              <el-option v-for="o in dicts.projectGroups" :key="o.id" :label="o.name" :value="o.id" />
            </el-select>
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item label="环境">
            <el-select v-model="form.environment" clearable style="width:100%">
              <el-option v-for="o in dicts.environments" :key="o.id" :label="o.name" :value="o.id" />
            </el-select>
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item label="渠道">
            <el-select v-model="form.channel" clearable style="width:100%">
              <el-option v-for="o in dicts.channels" :key="o.id" :label="o.name" :value="o.id" />
            </el-select>
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item label="云厂商">
            <el-select v-model="form.cloud_provider" clearable style="width:100%">
              <el-option v-for="o in dicts.cloudProviders" :key="o.id" :label="o.name" :value="o.id" />
            </el-select>
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item label="地域/机房">
            <el-input v-model="form.region" placeholder="如 cn-hangzhou" />
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item label="状态">
            <el-select v-model="form.status" style="width:100%">
              <el-option label="运行中" value="running" />
              <el-option label="已停止" value="stopped" />
              <el-option label="维护中" value="maintenance" />
            </el-select>
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item label="SSH 用户">
            <el-input v-model="form.ssh_user" placeholder="默认 root" />
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item label="SSH 端口">
            <el-input-number v-model="form.ssh_port" :min="1" :max="65535" style="width:100%" />
          </el-form-item>
        </el-col>
        <!-- 执行通道选项来自 Agent 管理接口，没有 agent 模块权限就不展示 -->
        <el-col v-if="canAgent" :span="12">
          <el-form-item label="执行通道">
            <el-select v-model="form.channel_mode" style="width:100%" @change="onChannelModeChange">
              <el-option label="自动（按路由规则）" value="auto" />
              <el-option label="强制直连" value="direct" />
              <el-option label="指定 Agent" value="agent" />
            </el-select>
            <div class="form-tip">
              自动 = 按「Agent 管理 → 通道路由」的项目组/环境规则解析，无规则命中则直连；
              自身有公网 IP 的管理服建议选「强制直连」，避免被项目级规则拖进 agent 通道
            </div>
          </el-form-item>
        </el-col>
        <el-col v-if="canAgent && form.channel_mode === 'agent'" :span="12">
          <el-form-item label="指定 Agent" prop="agent">
            <el-select v-model="form.agent" clearable placeholder="请选择 Agent" style="width:100%">
              <el-option v-for="o in agents" :key="o.id" :label="o.name" :value="o.id" />
            </el-select>
          </el-form-item>
        </el-col>
        <!-- 分配服务仅在新增时提供；已有主机的服务调整走「配置管理 → 服务器分配」 -->
        <el-col v-if="!editRow" :span="24">
          <el-form-item label="分配服务">
            <el-select v-model="form.service_type_ids" multiple clearable placeholder="可选：规划本机承载的服务类型" style="width:100%">
              <el-option v-for="o in dicts.serviceTypes" :key="o.id" :label="o.name" :value="o.id" />
            </el-select>
            <div class="form-tip">选择后自动创建「已规划」服务记录并生成进程名；未分配服务的主机不能作为任务执行目标</div>
          </el-form-item>
        </el-col>
        <el-col :span="24">
          <el-form-item label="备注">
            <el-input v-model="form.remark" type="textarea" :rows="2" />
          </el-form-item>
        </el-col>
      </el-row>
    </el-form>
    <template #footer>
      <el-button @click="dialogVisible = false">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="handleSubmit">确定</el-button>
    </template>
  </el-dialog>

  <!-- 批量编辑弹窗：仅勾选「修改」的字段会被提交 -->
  <el-dialog v-model="batchEditVisible" :title="`批量编辑（已选 ${selection.length} 台服务器）`" width="480px">
    <el-alert type="info" :closable="false" show-icon style="margin-bottom:16px">
      仅勾选「修改」的字段会应用到所有选中主机；归属维度（项目组/环境等）请走「配置管理 → 服务器分配」
    </el-alert>
    <el-form label-width="90px">
      <el-form-item v-for="field in batchFields" :key="field.key" :label="field.label">
        <div class="batch-line">
          <el-checkbox v-model="batchForm[field.key].on">修改</el-checkbox>
          <el-select
            v-if="field.key === 'status'"
            v-model="batchForm.status.value" :disabled="!batchForm.status.on" style="flex:1"
          >
            <el-option label="运行中" value="running" />
            <el-option label="已停止" value="stopped" />
            <el-option label="维护中" value="maintenance" />
          </el-select>
          <el-input-number
            v-else-if="field.key === 'ssh_port'"
            v-model="batchForm.ssh_port.value" :disabled="!batchForm.ssh_port.on"
            :min="1" :max="65535" style="flex:1"
          />
          <template v-else-if="field.key === 'channel_mode'">
            <el-select
              v-model="batchForm.channel_mode.value" :disabled="!batchForm.channel_mode.on"
              style="flex:1"
            >
              <el-option label="自动（按路由规则）" value="auto" />
              <el-option label="强制直连" value="direct" />
              <el-option label="指定 Agent" value="agent" />
            </el-select>
            <el-select
              v-if="batchForm.channel_mode.value === 'agent'"
              v-model="batchForm.channel_mode.agent" :disabled="!batchForm.channel_mode.on"
              placeholder="选择 Agent" style="flex:1"
            >
              <el-option v-for="o in agents" :key="o.id" :label="o.name" :value="o.id" />
            </el-select>
          </template>
          <el-input
            v-else
            v-model="batchForm[field.key].value" :disabled="!batchForm[field.key].on"
            :placeholder="field.placeholder"
          />
        </div>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="batchEditVisible = false">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="handleBatchEdit">确定</el-button>
    </template>
  </el-dialog>

  <!-- CSV 导入弹窗 -->
  <el-dialog v-model="importVisible" title="批量导入服务器" width="560px">
    <el-alert type="info" :closable="false" show-icon style="margin-bottom:12px">
      按模板填写（字典列填编码，如环境 prod；channel 为渠道字典）；执行通道 channel_mode 填
      auto/direct/agent，agent 列填 Agent 名称（仅 channel_mode=agent 时生效）；任一行校验失败则整体不导入并提示逐行错误
    </el-alert>
    <div class="import-toolbar">
      <el-button size="small" :icon="Download" @click="handleTemplateDownload">下载 CSV 模板</el-button>
      <el-radio-group v-model="importMode" size="small">
        <el-radio-button value="skip">主机名已存在时跳过</el-radio-button>
        <el-radio-button value="update">主机名已存在时更新</el-radio-button>
      </el-radio-group>
    </div>
    <el-upload
      ref="uploadRef"
      drag
      :auto-upload="false"
      :limit="1"
      accept=".csv"
      :on-change="(file) => (importFile = file.raw)"
      :on-remove="() => (importFile = null)"
    >
      <el-icon size="36" color="#c0c4cc"><Upload /></el-icon>
      <div class="el-upload__text">拖拽 CSV 文件到此处，或<em>点击选择</em></div>
    </el-upload>
    <el-table v-if="importErrors.length" :data="importErrors" size="small" max-height="220" style="margin-top:12px">
      <el-table-column prop="line" label="行号" width="70" />
      <el-table-column prop="name" label="主机名" width="140" />
      <el-table-column label="错误">
        <template #default="{ row }">{{ row.errors.join('；') }}</template>
      </el-table-column>
    </el-table>
    <template #footer>
      <el-button @click="importVisible = false">取消</el-button>
      <el-button type="primary" :loading="submitting" :disabled="!importFile" @click="handleImport">导入</el-button>
    </template>
  </el-dialog>

  <!-- JumpServer 注册结果弹窗 -->
  <el-dialog v-model="jsResultVisible" title="JumpServer 注册结果" width="620px">
    <el-alert
      :type="jsResult.failed ? 'warning' : 'success'" :closable="false" show-icon style="margin-bottom:12px"
    >
      共 {{ jsResult.total }} 台：成功 {{ jsResult.succeeded }}，失败 {{ jsResult.failed }}
    </el-alert>
    <el-table :data="jsResult.results" size="small" max-height="320">
      <el-table-column prop="name" label="主机名" min-width="120" />
      <el-table-column prop="ip" label="私网 IP" width="130" />
      <el-table-column label="结果" width="80">
        <template #default="{ row }">
          <el-tag size="small" :type="row.ok ? 'success' : 'danger'">{{ row.ok ? '成功' : '失败' }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="msg" label="说明" min-width="160" show-overflow-tooltip />
    </el-table>
    <template #footer>
      <el-button @click="jsResultVisible = false">关闭</el-button>
    </template>
  </el-dialog>

  <!-- 所在拓扑（影响面反查） -->
  <TopoImpactDialog
    v-model="impactVisible"
    kind="server"
    :ref-id="impactRow.id"
    :label="impactRow.name"
  />
</template>

<script setup>
import { ref, computed, reactive, onMounted, onUnmounted } from 'vue'
import { debounce } from '../../utils/debounce'
import { Plus, Edit, Delete, EditPen, Connection, Upload, Download } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  getServers, createServer, updateServer, deleteServer,
  batchUpdateServers, batchDeleteServers, registerJumpServer,
  exportServers, getServerImportTemplate, importServers,
  projectGroupApi, channelApi, environmentApi, cloudProviderApi, serviceTypeApi, fetchDictOptions,
} from '../../api/assets'
import { getAgents } from '../../api/agents'
import { useAuthStore } from '../../stores/auth'
import TopoImpactDialog from '../../components/TopoImpactDialog.vue'

const authStore = useAuthStore()
// 按钮可见性跟角色矩阵走（后端 roles.MODULES）：cmdb 写档才给增删改，
// 执行通道那两个字段的选项来自 Agent 接口，另按 agent 模块判
// 新增与改删分开判：cmdb 有「可读可新增」这一档，只读档不该看到新增按钮
const canCreate = computed(() => authStore.can('cmdb', 'create'))
const canWrite = computed(() => authStore.can('cmdb', 'write'))
const canAgent = computed(() => authStore.can('agent'))
// 拓扑入口另按 topo 模块判：没有画布权限的人看到一个点开就 403 的按钮只会困惑
const canTopo = computed(() => authStore.can('topo'))

const loading = ref(false)
const submitting = ref(false)
const tableData = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const filter = reactive({
  keyword: '', status: '', env: '', project_group: '', channel: '', cloud_provider: '',
})
const dialogVisible = ref(false)
const editRow = ref(null)
const formRef = ref()

// CMDB 字典下拉选项（仅启用项）
const dicts = reactive({ environments: [], projectGroups: [], channels: [], cloudProviders: [], serviceTypes: [] })

// 字典外键字段：提交前把清空的下拉值归一为 null
const FK_FIELDS = ['environment', 'project_group', 'channel', 'cloud_provider', 'agent']

// 执行通道候选（Agent 管理接口仅管理员可访问，普通用户不加载）
const agents = ref([])

// 操作系统/CPU/内存 下拉候选项（操作系统支持手动输入自定义值）
const OS_OPTIONS = [
  'CentOS 7.9', 'CentOS 8', 'Rocky Linux 9', 'AlmaLinux 9', 'openEuler 22.03',
  'Ubuntu 20.04', 'Ubuntu 22.04', 'Ubuntu 24.04', 'Debian 11', 'Debian 12',
  'Windows Server 2019', 'Windows Server 2022',
]
const CPU_OPTIONS = [1, 2, 4, 8, 16, 32, 64, 128]
const MEMORY_OPTIONS = [1024, 2048, 4096, 8192, 16384, 32768, 65536, 131072, 262144]

const emptyForm = () => ({
  name: '', private_ip: '', public_ip: '', os: '', cpu: null, memory: null, status: 'running',
  environment: null, project_group: null, channel: null, cloud_provider: null,
  region: '', ssh_user: 'root', ssh_port: 22, remark: '', service_type_ids: [],
  channel_mode: 'auto', agent: null,
})
const form = reactive(emptyForm())
const rules = {
  name: [{ required: true, message: '请输入名称' }],
  private_ip: [{ required: true, message: '请输入私网 IP' }],
}

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
  if (canAgent.value) {
    const res = await getAgents()
    agents.value = Array.isArray(res) ? res : res.data || []
  }
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

async function fetchData() {
  loading.value = true
  try {
    const params = { page: page.value, page_size: pageSize.value }
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

function openDialog(row = null) {
  editRow.value = row
  Object.assign(form, row ? { ...row } : emptyForm())
  // 未填写过的 cpu/memory（值为 0）在下拉框中显示占位提示而非 “0”
  if (row) {
    if (!form.cpu) form.cpu = null
    if (!form.memory) form.memory = null
  }
  dialogVisible.value = true
  formRef.value?.clearValidate()
}

// 切走「指定 Agent」时清空选中的 Agent，避免提交不生效的残留值
function onChannelModeChange(mode) {
  if (mode !== 'agent') form.agent = null
}

async function handleSubmit() {
  await formRef.value.validate()
  if (form.channel_mode === 'agent' && !form.agent) {
    ElMessage.warning('执行通道选择「指定 Agent」时必须选择 Agent')
    return
  }
  submitting.value = true
  try {
    const payload = { ...form }
    FK_FIELDS.forEach((key) => {
      if (payload[key] === '' || payload[key] === undefined) payload[key] = null
    })
    // 后端 cpu/memory 为整数字段（默认 0），os 为空字符串，清空下拉后归一化
    if (payload.cpu == null) payload.cpu = 0
    if (payload.memory == null) payload.memory = 0
    if (payload.os == null) payload.os = ''
    if (editRow.value) {
      await updateServer(editRow.value.id, payload)
      ElMessage.success('更新成功')
    } else {
      await createServer(payload)
      ElMessage.success('新增成功')
    }
    dialogVisible.value = false
    fetchData()
  } finally {
    submitting.value = false
  }
}

async function handleDelete(row) {
  await ElMessageBox.confirm(`确定删除服务器「${row.name}」吗？`, '警告', { type: 'warning' })
  await deleteServer(row.id)
  ElMessage.success('删除成功')
  fetchData()
}

// ── 批量操作 ─────────────────────────────────────────────
const selection = ref([])
const batchEditVisible = ref(false)
// 批量编辑字段定义与表单：仅勾选 on 的字段提交
const batchFields = [
  { key: 'status', label: '状态' },
  { key: 'os', label: '操作系统', placeholder: '如 Ubuntu 22.04（留空表示清空）' },
  { key: 'region', label: '地域/机房', placeholder: '如 cn-hangzhou（留空表示清空）' },
  { key: 'ssh_user', label: 'SSH 用户', placeholder: '如 root' },
  { key: 'ssh_port', label: 'SSH 端口' },
  { key: 'channel_mode', label: '执行通道' },
  { key: 'remark', label: '备注', placeholder: '留空表示清空' },
]
const batchForm = reactive({
  status: { on: false, value: 'running' },
  os: { on: false, value: '' },
  region: { on: false, value: '' },
  ssh_user: { on: false, value: 'root' },
  ssh_port: { on: false, value: 22 },
  channel_mode: { on: false, value: 'auto', agent: null },
  remark: { on: false, value: '' },
})

function openBatchEdit() {
  batchFields.forEach(({ key }) => (batchForm[key].on = false))
  batchEditVisible.value = true
}

async function handleBatchEdit() {
  const update = {}
  batchFields.forEach(({ key }) => {
    if (batchForm[key].on) update[key] = batchForm[key].value
  })
  if (!Object.keys(update).length) {
    ElMessage.warning('请至少勾选一个要修改的字段')
    return
  }
  if (update.channel_mode === 'agent') {
    if (!batchForm.channel_mode.agent) {
      ElMessage.warning('执行通道选择「指定 Agent」时必须选择 Agent')
      return
    }
    update.agent = batchForm.channel_mode.agent
  }
  submitting.value = true
  try {
    const res = await batchUpdateServers({ server_ids: selection.value.map((r) => r.id), update })
    ElMessage.success(`已更新 ${res.data?.updated ?? 0} 台服务器`)
    batchEditVisible.value = false
    selection.value = []
    fetchData()
  } finally {
    submitting.value = false
  }
}

async function handleBatchDelete() {
  await ElMessageBox.confirm(
    `确定删除选中的 ${selection.value.length} 台服务器吗？其服务规划记录将一并删除，且不可恢复。`,
    '警告',
    { type: 'warning', confirmButtonText: '删除', confirmButtonClass: 'el-button--danger' },
  )
  const res = await batchDeleteServers({ server_ids: selection.value.map((r) => r.id) })
  ElMessage.success(`已删除 ${res.data?.deleted ?? 0} 台服务器`)
  selection.value = []
  fetchData()
}

// ── JumpServer 注册 ─────────────────────────────────────
const jsResultVisible = ref(false)
const jsResult = ref({ total: 0, succeeded: 0, failed: 0, results: [] })

async function handleRegisterJumpServer() {
  await ElMessageBox.confirm(
    `将选中的 ${selection.value.length} 台服务器注册为 JumpServer 资产（已存在的自动跳过）？`,
    '提示',
    { type: 'info' },
  )
  submitting.value = true
  try {
    const res = await registerJumpServer({ server_ids: selection.value.map((r) => r.id) })
    jsResult.value = res.data || { total: 0, succeeded: 0, failed: 0, results: [] }
    jsResultVisible.value = true
  } finally {
    submitting.value = false
  }
}

// ── CSV 导入导出 ─────────────────────────────────────────
const importVisible = ref(false)
const importMode = ref('skip')
const importFile = ref(null)
const importErrors = ref([])
const uploadRef = ref()

// 影响面反查弹窗
const impactVisible = ref(false)
const impactRow = ref({})

function openImpact(row) {
  impactRow.value = row
  impactVisible.value = true
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

async function handleExport() {
  const params = {}
  Object.entries(filter).forEach(([key, value]) => {
    if (value) params[key] = value
  })
  const blob = await exportServers(params)
  downloadBlob(blob, 'servers.csv')
}

async function handleTemplateDownload() {
  downloadBlob(await getServerImportTemplate(), 'servers_import_template.csv')
}

function openImport() {
  importFile.value = null
  importErrors.value = []
  importMode.value = 'skip'
  uploadRef.value?.clearFiles()
  importVisible.value = true
}

async function handleImport() {
  const formData = new FormData()
  formData.append('file', importFile.value)
  formData.append('mode', importMode.value)
  submitting.value = true
  importErrors.value = []
  try {
    const res = await importServers(formData)
    const { created = 0, updated = 0, skipped = 0 } = res.data || {}
    ElMessage.success(`导入完成：新增 ${created}，更新 ${updated}，跳过 ${skipped}`)
    importVisible.value = false
    fetchData()
  } catch (error) {
    // 校验失败：后端返回逐行错误明细，展示在弹窗内便于修正
    importErrors.value = error.response?.data?.data?.errors || []
  } finally {
    submitting.value = false
  }
}

onMounted(() => {
  loadDicts()
  fetchData()
})
</script>

<style scoped>
.toolbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.toolbar-left { display: flex; gap: 8px; flex-wrap: wrap; }
.toolbar-right { display: flex; gap: 8px; align-items: center; flex-shrink: 0; flex-wrap: wrap; }
.form-tip { color: #909399; font-size: 12px; line-height: 1.5; margin-top: 4px; }
.batch-line { display: flex; gap: 10px; align-items: center; width: 100%; }
.muted { color: #c0c4cc; }
.channel-source { color: #909399; font-size: 12px; margin-left: 4px; }
.batch-line > :last-child { flex: 1; }
.import-toolbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
</style>
