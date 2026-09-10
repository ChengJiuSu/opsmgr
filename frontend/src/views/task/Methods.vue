<template>
  <el-card shadow="never">
    <div class="toolbar">
      <div class="toolbar-left">
        <el-input v-model="filter.keyword" placeholder="方法名称" clearable style="width:160px" @input="searchDebounced" @keyup.enter="handleSearch" @clear="handleSearch" />
        <el-select v-model="filter.provider" placeholder="Provider" clearable style="width:150px" @change="handleSearch">
          <el-option label="Ansible 剧本" value="ansible_playbook" />
          <el-option label="Jenkins 构建" value="jenkins" />
          <el-option label="JumpServer 注册" value="jumpserver" />
          <el-option label="云主机管理" value="cloud_host" />
        </el-select>
        <el-button :icon="Search" @click="fetchData">查询</el-button>
      </div>
      <el-button type="primary" :icon="Plus" @click="openDialog()">新增方法</el-button>
    </div>

    <el-alert type="info" :closable="false" show-icon style="margin-bottom:12px">
      平台方法用于集中管理可复用的执行能力。Ansible 剧本方法 = 指定剧本文件 + tags；Jenkins 构建方法 = 指定 Job 触发 CI（直连时连接信息在「系统配置」维护 jenkins.url / jenkins.user / jenkins.api_token；Jenkins 在内网时经 Agent 触发，凭据放 agent 本机 config.yaml）；JumpServer 注册方法 = 把目标主机注册为 JumpServer 资产（直连时连接信息在「系统配置」维护 jumpserver.url / jumpserver.token；JumpServer 在内网时经 Agent 注册，凭据放 agent 本机 config.yaml）；云主机管理等 provider 为后续扩展预留。
    </el-alert>

    <el-table v-loading="loading" :data="tableData" stripe>
      <el-table-column prop="name" label="名称" min-width="120" />
      <el-table-column prop="code" label="标识" width="150" />
      <el-table-column label="Provider" width="130">
        <template #default="{ row }">
          <el-tag size="small" :type="providerTagType(row.provider)">
            {{ row.provider_display }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="剧本 / Job" min-width="200">
        <template #default="{ row }">
          <span v-if="row.provider === 'ansible_playbook'">
            <code>{{ row.config?.playbook || '-' }}</code>
            <el-tag v-if="row.config?.tags" size="small" type="info" style="margin-left:6px">
              tags: {{ row.config.tags }}
            </el-tag>
          </span>
          <span v-else-if="row.provider === 'jenkins'">
            <el-tag size="small" type="info">job 由任务参数指定</el-tag>
            <el-tag v-if="Object.keys(row.config?.default_params || {}).length" size="small" type="info" style="margin-left:6px">
              默认参数 {{ Object.keys(row.config.default_params).length }} 项
            </el-tag>
            <el-tag v-if="row.config?.agent" size="small" type="warning" style="margin-left:6px">
              Agent {{ row.config.agent }}
            </el-tag>
          </span>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column label="服务类型" width="110">
        <template #default="{ row }">
          <el-tag v-if="row.service_type_name" size="small" type="warning">{{ row.service_type_name }}</el-tag>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column prop="description" label="说明" show-overflow-tooltip />
      <el-table-column label="状态" width="80">
        <template #default="{ row }">
          <el-tag size="small" :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '启用' : '禁用' }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="140" fixed="right">
        <template #default="{ row }">
          <el-button size="small" :icon="Edit" @click="openDialog(row)">编辑</el-button>
          <el-button size="small" type="danger" :icon="Delete" @click="handleDelete(row)" />
        </template>
      </el-table-column>
    </el-table>
  </el-card>

  <el-dialog v-model="dialogVisible" :title="editRow ? '编辑方法' : '新增方法'" width="560px">
    <el-form ref="formRef" :model="form" :rules="rules" label-width="100px">
      <el-form-item label="名称" prop="name">
        <el-input v-model="form.name" />
      </el-form-item>
      <el-form-item label="标识" prop="code">
        <el-input v-model="form.code" :disabled="!!editRow" placeholder="唯一英文标识，如 restart-nginx" />
      </el-form-item>
      <el-form-item label="Provider">
        <el-select v-model="form.provider" style="width:100%">
          <el-option label="Ansible 剧本" value="ansible_playbook" />
          <el-option label="Jenkins 构建" value="jenkins" />
          <el-option label="JumpServer 注册" value="jumpserver" />
          <el-option label="云主机管理（预留）" value="cloud_host" />
        </el-select>
      </el-form-item>

      <!-- ansible_playbook 专属配置 -->
      <template v-if="form.provider === 'ansible_playbook'">
        <el-form-item label="剧本文件" prop="config_playbook">
          <el-input v-model="form.config.playbook" placeholder="相对 Ansible 项目目录，如 site.yml" />
        </el-form-item>
        <el-form-item label="Tags">
          <el-input v-model="form.config.tags" placeholder="逗号分隔，如 nginx,restart" />
        </el-form-item>
        <el-form-item label="Inventory">
          <el-input v-model="form.config.inventory" placeholder="可留空用项目默认" />
        </el-form-item>
        <el-form-item label="默认 Limit">
          <el-input v-model="form.config.limit" placeholder="可留空" />
        </el-form-item>
      </template>

      <!-- jenkins 专属配置：不绑定具体 job，job_name 在任务模块参数中指定 -->
      <template v-else-if="form.provider === 'jenkins'">
        <el-form-item label="默认参数">
          <el-input v-model="rawParams" type="textarea" :rows="4" placeholder='JSON 键值对，如 {"BRANCH": "master"}；任务模块 params 可覆盖' />
        </el-form-item>
        <el-form-item label="UNSTABLE">
          <el-switch v-model="form.config.allow_unstable" />
          <span class="form-tip">开启后 UNSTABLE 构建结果视为成功</span>
        </el-form-item>
        <el-form-item label="执行 Agent">
          <el-input v-model="form.config.agent" placeholder="留空=按系统配置 jenkins.agent 与通道路由解析" />
          <span class="form-tip">填 Agent 名则本方法固定经该 Agent 触发（Jenkins 在内网时用）</span>
        </el-form-item>
        <el-alert type="warning" :closable="false" style="margin-bottom:18px">
          Jenkins 方法为公共方法，不绑定具体 job：任务模块的方法参数中通过
          <code>"job_name"</code> 指定要触发的 job；模块参数 <code>"agent"</code>
          可临时覆盖执行通道。
        </el-alert>
      </template>

      <!-- jumpserver：仅执行通道可配，把模块目标主机注册为 JumpServer 资产 -->
      <template v-else-if="form.provider === 'jumpserver'">
        <el-form-item label="执行 Agent">
          <el-input v-model="form.config.agent" placeholder="留空=按系统配置 jumpserver.agent 与主机执行通道解析" />
          <span class="form-tip">填 Agent 名则本方法固定经该 Agent 注册（JumpServer 在内网时用）</span>
        </el-form-item>
        <el-alert type="info" :closable="false" style="margin-bottom:18px">
          执行时把模块匹配到的目标主机注册为 JumpServer 资产（已存在的自动跳过）。
          直连时连接信息在「系统配置」维护 <code>jumpserver.url</code> / <code>jumpserver.token</code>；
          JumpServer 在内网时经 Agent 注册，凭据放 agent 本机 config.yaml。
          留空则按每台主机自身的执行通道分组，模块参数 <code>"agent"</code> 可临时覆盖。
        </el-alert>
      </template>

      <!-- 其他 provider：通用 JSON 配置 -->
      <el-form-item v-else label="配置 (JSON)">
        <el-input v-model="rawConfig" type="textarea" :rows="4" placeholder='{"action": "reboot"}' />
      </el-form-item>

      <el-form-item label="服务类型">
        <el-select v-model="form.service_type" clearable style="width:100%" placeholder="部署类方法关联后，执行前校验目标主机已规划该服务">
          <el-option v-for="o in serviceTypes" :key="o.id" :label="o.name" :value="o.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="说明">
        <el-input v-model="form.description" />
      </el-form-item>
      <el-form-item label="启用">
        <el-switch v-model="form.enabled" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="dialogVisible = false">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="handleSubmit">确定</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { ref, reactive, onMounted, onUnmounted } from 'vue'
import { debounce } from '../../utils/debounce'
import { Plus, Edit, Delete, Search } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { getMethods, createMethod, updateMethod, deleteMethod } from '../../api/tasks'
import { serviceTypeApi, fetchDictOptions } from '../../api/assets'

const loading = ref(false)
const submitting = ref(false)
const tableData = ref([])
const filter = reactive({ keyword: '', provider: '' })
// 服务类型下拉选项（部署前置校验关联用）
const serviceTypes = ref([])

const dialogVisible = ref(false)
const editRow = ref(null)
const formRef = ref()
const rawConfig = ref('{}')
// jenkins 默认构建参数（JSON 文本，提交时解析进 config.default_params）
const rawParams = ref('{}')
const emptyForm = () => ({
  name: '', code: '', provider: 'ansible_playbook', description: '', service_type: null,
  enabled: true,
  config: { playbook: '', tags: '', inventory: '', limit: '', allow_unstable: false, agent: '' },
})
const form = reactive(emptyForm())
const rules = {
  name: [{ required: true, message: '请输入名称' }],
  code: [{ required: true, message: '请输入标识' }],
  config_playbook: [{
    validator: (_r, _v, cb) =>
      (form.provider !== 'ansible_playbook' || form.config.playbook)
        ? cb() : cb(new Error('请填写剧本文件')),
  }],
}

function providerTagType(provider) {
  return { ansible_playbook: 'success', jenkins: 'primary', jumpserver: 'danger' }[provider] || 'warning'
}

// 本页不分页（一次取全量），搜索直接重查
function handleSearch() {
  searchDebounced.cancel()
  fetchData()
}

// 边输边查（300ms 防抖），无需回车
const searchDebounced = debounce(handleSearch)
onUnmounted(() => searchDebounced.cancel())

async function fetchData() {
  loading.value = true
  try {
    const params = { page_size: 200 }
    if (filter.keyword) params.keyword = filter.keyword
    if (filter.provider) params.provider = filter.provider
    const res = await getMethods(params)
    tableData.value = res.data?.results || []
  } finally {
    loading.value = false
  }
}

function openDialog(row = null) {
  editRow.value = row
  Object.assign(form, emptyForm())
  if (row) {
    Object.assign(form, {
      name: row.name, code: row.code, provider: row.provider,
      description: row.description, enabled: row.enabled, service_type: row.service_type,
      config: {
        playbook: '', tags: '', inventory: '', limit: '', allow_unstable: false, agent: '',
        ...(row.config || {}),
      },
    })
    rawConfig.value = JSON.stringify(row.config || {}, null, 2)
    rawParams.value = JSON.stringify(row.config?.default_params || {}, null, 2)
  } else {
    rawConfig.value = '{}'
    rawParams.value = '{}'
  }
  dialogVisible.value = true
  formRef.value?.clearValidate()
}

async function handleSubmit() {
  await formRef.value.validate()
  let config = form.config
  if (form.provider === 'jenkins') {
    let defaultParams
    try {
      defaultParams = JSON.parse(rawParams.value || '{}')
    } catch {
      ElMessage.error('默认参数 JSON 格式不正确')
      return
    }
    if (defaultParams === null || Array.isArray(defaultParams) || typeof defaultParams !== 'object') {
      ElMessage.error('默认参数必须为 JSON 键值对对象')
      return
    }
    config = {
      default_params: defaultParams,
      allow_unstable: !!form.config.allow_unstable,
    }
    // 留空即不写 agent 键：解析回落到系统配置与通道路由
    const agent = (form.config.agent || '').trim()
    if (agent) config.agent = agent
  } else if (form.provider === 'jumpserver') {
    // 只有执行通道可配；留空即不写 agent 键，回落系统配置与主机执行通道
    const agent = (form.config.agent || '').trim()
    config = agent ? { agent } : {}
  } else if (form.provider !== 'ansible_playbook') {
    try {
      config = JSON.parse(rawConfig.value || '{}')
    } catch {
      ElMessage.error('配置 JSON 格式不正确')
      return
    }
  }
  const payload = {
    name: form.name, code: form.code, provider: form.provider,
    description: form.description, enabled: form.enabled, config,
    // clearable 置空时为 undefined/空串，统一提交 null 表示不关联
    service_type: form.service_type || null,
  }
  submitting.value = true
  try {
    if (editRow.value) {
      await updateMethod(editRow.value.id, payload)
      ElMessage.success('更新成功')
    } else {
      await createMethod(payload)
      ElMessage.success('新增成功')
    }
    dialogVisible.value = false
    fetchData()
  } finally {
    submitting.value = false
  }
}

async function handleDelete(row) {
  await ElMessageBox.confirm(`确定删除方法「${row.name}」吗？`, '警告', { type: 'warning' })
  await deleteMethod(row.id)
  ElMessage.success('删除成功')
  fetchData()
}

onMounted(() => {
  fetchData()
  fetchDictOptions(serviceTypeApi).then((res) => {
    serviceTypes.value = res.data?.results || []
  })
})
</script>

<style scoped>
.toolbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.toolbar-left { display: flex; gap: 8px; }
.muted { color: #c0c4cc; }
.form-tip { margin-left: 8px; color: #909399; font-size: 12px; }
code { background: #f5f7fa; padding: 1px 6px; border-radius: 3px; font-size: 12px; }
</style>
