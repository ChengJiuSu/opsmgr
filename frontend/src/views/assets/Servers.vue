<template>
  <el-card shadow="never">
    <!-- 工具栏 -->
    <div class="toolbar">
      <div class="toolbar-left">
        <el-select v-model="filter.status" placeholder="状态" clearable style="width:110px" @change="fetchData">
          <el-option label="运行中" value="running" />
          <el-option label="已停止" value="stopped" />
          <el-option label="维护中" value="maintenance" />
        </el-select>
        <el-select v-model="filter.env" placeholder="环境" clearable style="width:110px" @change="fetchData">
          <el-option v-for="o in dicts.environments" :key="o.id" :label="o.name" :value="o.code" />
        </el-select>
        <el-select v-model="filter.project_group" placeholder="项目组" clearable style="width:130px" @change="fetchData">
          <el-option v-for="o in dicts.projectGroups" :key="o.id" :label="o.name" :value="o.code" />
        </el-select>
        <el-select v-model="filter.channel" placeholder="渠道" clearable style="width:120px" @change="fetchData">
          <el-option v-for="o in dicts.channels" :key="o.id" :label="o.name" :value="o.code" />
        </el-select>
        <el-select v-model="filter.cloud_provider" placeholder="云厂商" clearable style="width:120px" @change="fetchData">
          <el-option v-for="o in dicts.cloudProviders" :key="o.id" :label="o.name" :value="o.code" />
        </el-select>
      </div>
      <el-button type="primary" :icon="Plus" @click="openDialog()">新增服务器</el-button>
    </div>

    <!-- 表格 -->
    <el-table v-loading="loading" :data="tableData" stripe>
      <el-table-column prop="name" label="名称" min-width="120" />
      <el-table-column prop="ip" label="IP 地址" width="130" />
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
      <el-table-column v-if="authStore.isStaff" label="操作" width="140" fixed="right">
        <template #default="{ row }">
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
  <el-dialog v-model="dialogVisible" :title="editRow ? '编辑服务器' : '新增服务器'" width="560px">
    <el-form ref="formRef" :model="form" :rules="rules" label-width="90px">
      <el-row :gutter="16">
        <el-col :span="12">
          <el-form-item label="名称" prop="name">
            <el-input v-model="form.name" />
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item label="IP 地址" prop="ip">
            <el-input v-model="form.ip" />
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
          <el-form-item label="环境">
            <el-select v-model="form.environment" clearable style="width:100%">
              <el-option v-for="o in dicts.environments" :key="o.id" :label="o.name" :value="o.id" />
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
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { Plus, Edit, Delete } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  getServers, createServer, updateServer, deleteServer,
  projectGroupApi, channelApi, environmentApi, cloudProviderApi, serviceTypeApi, fetchDictOptions,
} from '../../api/assets'
import { useAuthStore } from '../../stores/auth'

const authStore = useAuthStore()

const loading = ref(false)
const submitting = ref(false)
const tableData = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const filter = reactive({ status: '', env: '', project_group: '', channel: '', cloud_provider: '' })
const dialogVisible = ref(false)
const editRow = ref(null)
const formRef = ref()

// CMDB 字典下拉选项（仅启用项）
const dicts = reactive({ environments: [], projectGroups: [], channels: [], cloudProviders: [], serviceTypes: [] })

// 字典外键字段：提交前把清空的下拉值归一为 null
const FK_FIELDS = ['environment', 'project_group', 'channel', 'cloud_provider']

// 操作系统/CPU/内存 下拉候选项（操作系统支持手动输入自定义值）
const OS_OPTIONS = [
  'CentOS 7.9', 'CentOS 8', 'Rocky Linux 9', 'AlmaLinux 9', 'openEuler 22.03',
  'Ubuntu 20.04', 'Ubuntu 22.04', 'Ubuntu 24.04', 'Debian 11', 'Debian 12',
  'Windows Server 2019', 'Windows Server 2022',
]
const CPU_OPTIONS = [1, 2, 4, 8, 16, 32, 64, 128]
const MEMORY_OPTIONS = [1024, 2048, 4096, 8192, 16384, 32768, 65536, 131072, 262144]

const emptyForm = () => ({
  name: '', ip: '', os: '', cpu: null, memory: null, status: 'running',
  environment: null, project_group: null, channel: null, cloud_provider: null,
  region: '', ssh_user: 'root', ssh_port: 22, remark: '', service_type_ids: [],
})
const form = reactive(emptyForm())
const rules = {
  name: [{ required: true, message: '请输入名称' }],
  ip: [{ required: true, message: '请输入 IP 地址' }],
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
}

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

async function handleSubmit() {
  await formRef.value.validate()
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

onMounted(() => {
  loadDicts()
  fetchData()
})
</script>

<style scoped>
.toolbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.toolbar-left { display: flex; gap: 8px; flex-wrap: wrap; }
.form-tip { color: #909399; font-size: 12px; line-height: 1.5; margin-top: 4px; }
</style>
