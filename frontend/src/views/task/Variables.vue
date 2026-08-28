<template>
  <el-card shadow="never">
    <div class="toolbar">
      <div class="toolbar-left">
        <el-input v-model="filter.keyword" placeholder="变量名 / 说明" clearable style="width:170px" @input="searchDebounced" @keyup.enter="handleSearch" @clear="handleSearch" />
        <el-select v-model="filter.project_group" placeholder="项目组" clearable filterable style="width:130px" @change="handleSearch">
          <el-option v-for="d in dictOptions.projects" :key="d.id" :label="d.name" :value="d.id" />
        </el-select>
        <el-select v-model="filter.channel" placeholder="渠道" clearable filterable style="width:120px" @change="handleSearch">
          <el-option v-for="d in dictOptions.channels" :key="d.id" :label="d.name" :value="d.id" />
        </el-select>
        <el-select v-model="filter.env" placeholder="环境" clearable filterable style="width:120px" @change="handleSearch">
          <el-option v-for="d in dictOptions.envs" :key="d.id" :label="d.name" :value="d.id" />
        </el-select>
        <el-select v-model="filter.enabled" placeholder="状态" clearable style="width:100px" @change="handleSearch">
          <el-option label="启用" value="true" />
          <el-option label="停用" value="false" />
        </el-select>
        <el-button :icon="Search" @click="handleSearch">查询</el-button>
        <el-button :icon="Refresh" @click="fetchData">刷新</el-button>
      </div>
      <el-button v-if="canWrite" type="primary" :icon="Plus" @click="openDialog()">新增变量</el-button>
    </div>

    <el-alert type="info" :closable="false" style="margin-bottom: 12px">
      变量按<b>项目组/渠道/环境</b>生效：任务执行时仅加载与模块有效维度精确匹配的启用变量，
      YAML 解析后以 <code>--extra-vars</code> 注入（命令内可用 <code v-pre>{{ 变量名 }}</code> 引用），
      模块自身的额外变量覆盖同名项。每次保存自动留存版本（保留最近 10 版，可回滚）。
    </el-alert>

    <el-table v-loading="loading" :data="tableData" stripe>
      <el-table-column prop="key" label="变量名" min-width="150" show-overflow-tooltip>
        <template #default="{ row }">
          <code>{{ row.key }}</code>
          <el-tag v-if="row.is_secret" size="small" type="warning" class="tag-gap">敏感</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="变量值（YAML）" min-width="200" show-overflow-tooltip>
        <template #default="{ row }">
          <span v-if="row.is_secret" class="mono muted">
            ●●●●●●（加密存储，不回显{{ row.has_value ? '' : ' · 未设置' }}）
          </span>
          <span v-else class="mono">{{ (row.value || '').slice(0, 80) || '—' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="项目组 / 渠道 / 环境" min-width="170" show-overflow-tooltip>
        <template #default="{ row }">
          {{ row.project_group_name }} / {{ row.channel_name }} / {{ row.environment_name }}
        </template>
      </el-table-column>
      <el-table-column prop="description" label="说明" min-width="130" show-overflow-tooltip>
        <template #default="{ row }"><span :class="{ unset: !row.description }">{{ row.description || '—' }}</span></template>
      </el-table-column>
      <el-table-column label="状态" width="80">
        <template #default="{ row }">
          <el-tag size="small" :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '启用' : '停用' }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="updated_at" label="更新时间" width="160">
        <template #default="{ row }">{{ formatTime(row.updated_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="310" fixed="right">
        <template #default="{ row }">
          <el-button size="small" :icon="Clock" @click="openHistory(row)">历史</el-button>
          <el-button
            v-if="row.is_secret && canWrite && row.has_value"
            size="small" :icon="View" @click="handleReveal(row)"
          >查看真值</el-button>
          <el-button v-if="canWrite" size="small" :icon="EditPen" @click="openDialog(row)">编辑</el-button>
          <el-button v-if="canWrite" size="small" type="danger" :icon="Delete" @click="handleDelete(row)" />
        </template>
      </el-table-column>
    </el-table>

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

  <!-- 新增 / 编辑弹窗：加宽 + 30 行 YAML 编辑区 -->
  <el-dialog v-model="dialogVisible" :title="form.id ? '编辑变量' : '新增变量'" width="960px" top="3vh">
    <el-form ref="formRef" :model="form" :rules="rules" label-width="90px">
      <el-row :gutter="16">
        <el-col :span="8">
          <el-form-item label="变量名" prop="key">
            <el-input v-model="form.key" placeholder="如 app_conf" />
          </el-form-item>
        </el-col>
        <el-col :span="10">
          <el-form-item label="说明">
            <el-input v-model="form.description" placeholder="用途说明，可留空" />
          </el-form-item>
        </el-col>
        <el-col :span="3">
          <el-form-item label="启用">
            <el-switch v-model="form.enabled" />
          </el-form-item>
        </el-col>
        <el-col :span="3">
          <el-form-item label="敏感">
            <el-switch v-model="form.is_secret" />
          </el-form-item>
        </el-col>
      </el-row>
      <el-alert
        v-if="form.is_secret" type="warning" :closable="false" show-icon
        style="margin-bottom:12px"
      >
        敏感变量：值加密存储、接口与历史版本均不回显，执行输出与现场归档中自动打码。
        值按<b>字面字符串</b>注入（不做 YAML 解析，密码里的 <code>#</code>、<code>:</code> 原样保留），
        ansible 侧拿到的仍是明文，剧本无需改动。
      </el-alert>
      <el-form-item label="生效条件" required>
        <div class="dim-filter">
          <el-select v-model="form.project_group" placeholder="项目组（必选）" filterable>
            <el-option v-for="d in dictOptions.projects" :key="d.id" :label="d.name" :value="d.id" />
          </el-select>
          <el-select v-model="form.channel" placeholder="渠道（必选）" filterable>
            <el-option v-for="d in dictOptions.channels" :key="d.id" :label="d.name" :value="d.id" />
          </el-select>
          <el-select v-model="form.environment" placeholder="环境（必选）" filterable>
            <el-option v-for="d in dictOptions.envs" :key="d.id" :label="d.name" :value="d.id" />
          </el-select>
        </div>
        <div class="form-tip">任务执行时按模块有效维度（项目组/渠道/环境）精确匹配加载该变量</div>
      </el-form-item>
      <el-form-item label="变量值" prop="value">
        <el-input
          v-if="form.is_secret" v-model="form.value" type="password" show-password
          :placeholder="form.id ? '留空表示不修改现有值' : '敏感值（如数据库密码），原样注入'"
        />
        <el-input
          v-else v-model="form.value" type="textarea" :rows="30" class="yaml-input"
          placeholder="YAML 格式，如：
mode: prod
db:
  host: 10.0.0.10
  port: 3306
ports:
  - 80
  - 443"
        />
        <div v-if="form.is_secret" class="form-tip">
          剧本/命令中以 <code v-pre>{{ 变量名 }}</code> 引用（值是字符串，无子键）
        </div>
        <div v-else class="form-tip">YAML 格式（保存时校验）；解析后的结构在剧本/命令中以 <code v-pre>{{ 变量名.子键 }}</code> 引用</div>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="dialogVisible = false">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="handleSubmit">保存</el-button>
    </template>
  </el-dialog>

  <!-- 历史版本抽屉：左列表右预览，可回滚 -->
  <el-drawer v-model="historyVisible" :title="`历史版本 — ${historyVar?.key || ''}（保留最近 10 版）`" size="55%">
    <div class="history-body">
      <div class="history-list">
        <el-table :data="versions" size="small" highlight-current-row @current-change="(v) => (currentVersion = v)">
          <el-table-column prop="version" label="版本" width="60">
            <template #default="{ row }">v{{ row.version }}</template>
          </el-table-column>
          <el-table-column label="时间" min-width="130">
            <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
          </el-table-column>
          <el-table-column prop="created_by_name" label="操作人" width="80" />
          <el-table-column label="状态" width="70">
            <template #default="{ row }">
              <el-tag size="small" :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '启用' : '停用' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column v-if="canWrite" label="操作" width="80">
            <template #default="{ row }">
              <el-button size="small" type="warning" text :disabled="row.version === versions[0]?.version" @click="handleRollback(row)">回滚</el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>
      <div class="history-preview">
        <template v-if="currentVersion">
          <div class="form-tip" style="margin-bottom:6px">
            v{{ currentVersion.version }} · {{ currentVersion.project_group_name || '—' }} /
            {{ currentVersion.channel_name || '—' }} / {{ currentVersion.environment_name || '—' }}
            <span v-if="currentVersion.description"> · {{ currentVersion.description }}</span>
          </div>
          <pre v-if="currentVersion.is_secret" class="yaml-preview muted">
●●●●●●（敏感变量，历史版本同样不回显）</pre>
          <pre v-else class="yaml-preview">{{ currentVersion.value || '（空值）' }}</pre>
        </template>
        <el-empty v-else description="点击左侧版本查看内容" :image-size="60" />
      </div>
    </div>
  </el-drawer>
</template>

<script setup>
import { ref, computed, reactive, onMounted, onUnmounted } from 'vue'
import { debounce } from '../../utils/debounce'
import { Plus, Delete, Search, Refresh, EditPen, Clock, View } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useAuthStore } from '../../stores/auth'
import { fetchDictOptions, projectGroupApi, channelApi, environmentApi } from '../../api/assets'
import {
  getVariables, createVariable, updateVariable, deleteVariable,
  getVariableVersions, rollbackVariable, revealVariable,
} from '../../api/tasks'

const authStore = useAuthStore()
// 按钮可见性跟角色矩阵走（变量含敏感真值，与任务流定义分开授权）
const canWrite = computed(() => authStore.can('variable', 'write'))
const loading = ref(false)
const submitting = ref(false)
const tableData = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const filter = reactive({ keyword: '', enabled: '', project_group: '', channel: '', env: '' })
const dictOptions = reactive({ projects: [], channels: [], envs: [] })

const formatTime = (t) => (t ? t.slice(0, 19).replace('T', ' ') : '')

const dialogVisible = ref(false)
const formRef = ref()
const emptyForm = () => ({
  id: null, key: '', value: '', description: '', enabled: true, is_secret: false,
  project_group: null, channel: null, environment: null,
})
const form = reactive(emptyForm())
const rules = {
  key: [
    { required: true, message: '请输入变量名' },
    { pattern: /^[a-zA-Z_][a-zA-Z0-9_]*$/, message: '须以字母/下划线开头，仅含字母、数字、下划线' },
  ],
}

async function loadDictOptions() {
  if (dictOptions.projects.length) return
  const [proj, chan, env] = await Promise.allSettled([
    fetchDictOptions(projectGroupApi), fetchDictOptions(channelApi), fetchDictOptions(environmentApi),
  ])
  if (proj.status === 'fulfilled') dictOptions.projects = proj.value.data?.results || []
  if (chan.status === 'fulfilled') dictOptions.channels = chan.value.data?.results || []
  if (env.status === 'fulfilled') dictOptions.envs = env.value.data?.results || []
}

async function fetchData() {
  loading.value = true
  try {
    const params = { page: page.value, page_size: pageSize.value }
    for (const k of ['keyword', 'enabled', 'project_group', 'channel', 'env']) {
      if (filter[k]) params[k] = filter[k]
    }
    const res = await getVariables(params)
    tableData.value = res.data?.results || []
    total.value = res.data?.total || 0
  } finally {
    loading.value = false
  }
}

function handleSearch() {
  searchDebounced.cancel()
  page.value = 1
  fetchData()
}

// 边输边查（300ms 防抖），无需回车
const searchDebounced = debounce(handleSearch)
onUnmounted(() => searchDebounced.cancel())

function openDialog(row) {
  Object.assign(form, emptyForm(), row ? {
    // 敏感变量的 value 后端不回显（这里恒为空串），留空提交表示不修改现有值
    id: row.id, key: row.key, value: row.value, description: row.description, enabled: row.enabled,
    is_secret: row.is_secret,
    project_group: row.project_group || null, channel: row.channel || null, environment: row.environment || null,
  } : {})
  dialogVisible.value = true
  formRef.value?.clearValidate()
}

async function handleReveal(row) {
  await ElMessageBox.confirm(
    `确定查看「${row.key}」的真值吗？该操作需二次验证，并会记入操作审计。`,
    '查看敏感变量', { type: 'warning' },
  )
  const res = await revealVariable(row.id)
  await ElMessageBox.alert(res.data?.value || '（空值）', `${row.key} 的真值`, {
    confirmButtonText: '已知悉',
    customClass: 'reveal-box',
  })
}

async function handleSubmit() {
  await formRef.value.validate()
  if (!form.project_group || !form.channel || !form.environment) {
    ElMessage.error('生效条件的项目组/渠道/环境必选，请选齐后再保存')
    return
  }
  submitting.value = true
  try {
    const payload = {
      key: form.key, value: form.value, description: form.description, enabled: form.enabled,
      is_secret: form.is_secret,
      project_group: form.project_group, channel: form.channel, environment: form.environment,
    }
    // 编辑敏感变量时值留空 = 保持原值：不删掉这个字段会把密码清成空串
    if (form.id && form.is_secret && !form.value) delete payload.value
    if (form.id) {
      await updateVariable(form.id, payload)
      ElMessage.success('变量已保存（已留存历史版本）')
    } else {
      await createVariable(payload)
      ElMessage.success('变量已创建')
    }
    dialogVisible.value = false
    fetchData()
  } finally {
    submitting.value = false
  }
}

async function handleDelete(row) {
  await ElMessageBox.confirm(
    `确定删除变量「${row.key}」及其全部历史版本吗？执行中引用该变量的命令/剧本将取不到值。`, '警告', { type: 'warning' },
  )
  await deleteVariable(row.id)
  ElMessage.success('删除成功')
  fetchData()
}

// ── 历史版本与回滚 ──────────────────────────────────────
const historyVisible = ref(false)
const historyVar = ref(null)
const versions = ref([])
const currentVersion = ref(null)

async function openHistory(row) {
  historyVar.value = row
  versions.value = []
  currentVersion.value = null
  historyVisible.value = true
  const res = await getVariableVersions(row.id)
  versions.value = res.data || []
  currentVersion.value = versions.value[0] || null
}

async function handleRollback(version) {
  await ElMessageBox.confirm(
    `确定回滚变量「${historyVar.value.key}」到 v${version.version}（${formatTime(version.created_at)}）吗？`
    + '当前内容将被覆盖，并自动留存为新版本。',
    '回滚确认', { type: 'warning' },
  )
  const res = await rollbackVariable(historyVar.value.id, version.version)
  ElMessage.success(res.msg || '已回滚')
  fetchData()
  openHistory(historyVar.value) // 刷新版本列表（回滚会生成新版本）
}

onMounted(() => {
  fetchData()
  loadDictOptions()
})
</script>

<style scoped>
.toolbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.toolbar-left { display: flex; gap: 8px; flex-wrap: wrap; }
.unset { color: #c0c4cc; }
.tag-gap { margin-left: 6px; }
.muted { color: #c0c4cc; }
.mono { font-family: 'Courier New', monospace; font-size: 12px; }
code { background: #f5f7fa; padding: 1px 6px; border-radius: 3px; font-size: 12px; }
.form-tip { color: #909399; font-size: 12px; line-height: 1.5; margin-top: 4px; }
.dim-filter { display: flex; gap: 8px; width: 100%; }
.dim-filter .el-select { flex: 1; }
.yaml-input :deep(textarea) { font-family: 'Courier New', monospace; font-size: 13px; line-height: 1.5; }
.history-body { display: flex; gap: 14px; height: 100%; }
.history-list { width: 46%; min-width: 360px; }
.history-preview { flex: 1; min-width: 0; }
.yaml-preview {
  background: #1e1e1e;
  color: #d4d4d4;
  padding: 12px;
  border-radius: 6px;
  font-family: 'Courier New', monospace;
  font-size: 13px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: calc(100vh - 200px);
  overflow-y: auto;
  margin: 0;
}
</style>
