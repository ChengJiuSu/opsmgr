<template>
  <el-card shadow="never">
    <!-- 工具栏 -->
    <div class="toolbar">
      <div class="toolbar-left">
        <el-input
          v-model="filter.keyword"
          placeholder="搜索画布名称 / 标识"
          clearable
          style="width:200px"
          @input="searchDebounced"
          @keyup.enter="handleSearch"
          @clear="handleSearch"
        />
        <el-select v-model="filter.project_group" placeholder="项目组" clearable style="width:150px" @change="handleSearch">
          <el-option v-for="i in dicts.projectGroups" :key="i.id" :label="i.name" :value="i.code" />
        </el-select>
        <el-select v-model="filter.channel" placeholder="渠道" clearable style="width:130px" @change="handleSearch">
          <el-option v-for="i in dicts.channels" :key="i.id" :label="i.name" :value="i.code" />
        </el-select>
        <el-select v-model="filter.env" placeholder="环境" clearable style="width:120px" @change="handleSearch">
          <el-option v-for="i in dicts.environments" :key="i.id" :label="i.name" :value="i.code" />
        </el-select>
      </div>
      <div class="toolbar-right">
        <el-button v-if="canCreate" :icon="Upload" @click="openImport">导入 JSON</el-button>
        <el-button v-if="canCreate" type="primary" :icon="Plus" @click="openDialog()">新建画布</el-button>
      </div>
    </div>

    <!-- 表格 -->
    <el-table v-loading="loading" :data="tableData" stripe @row-dblclick="openEditor">
      <el-table-column prop="name" label="画布名称" min-width="150">
        <template #default="{ row }">
          <el-link type="primary" @click="openEditor(row)">{{ row.name }}</el-link>
        </template>
      </el-table-column>
      <el-table-column prop="slug" label="标识" min-width="130">
        <template #default="{ row }"><span class="mono">{{ row.slug }}</span></template>
      </el-table-column>
      <el-table-column label="维度" min-width="240">
        <template #default="{ row }">
          <el-tag size="small" type="info">{{ row.project_group_name }}</el-tag>
          <el-tag size="small" type="info" style="margin-left:4px">{{ row.channel_name }}</el-tag>
          <el-tag size="small" :type="row.is_prod ? 'danger' : 'success'" style="margin-left:4px">
            {{ row.environment_name }}
          </el-tag>
          <span v-if="row.region" class="mono region">{{ row.region }}</span>
          <span v-else class="unset">跨地域</span>
        </template>
      </el-table-column>
      <el-table-column label="规模" width="120">
        <template #default="{ row }">
          <span class="muted">{{ row.node_count }} 节点 / {{ row.edge_count }} 连线</span>
        </template>
      </el-table-column>
      <el-table-column label="健康" width="120">
        <template #default="{ row }">
          <!-- 健康度是单独一发请求补上的，没回来之前显示占位而不是「正常」：
               把「还不知道」显示成「没问题」是最不该有的谎 -->
          <span v-if="!health[row.id]" class="unset">—</span>
          <el-tag v-else-if="health[row.id].danger" size="small" type="danger">
            {{ health[row.id].danger }} 异常
          </el-tag>
          <el-tag v-else-if="health[row.id].warning" size="small" type="warning">
            {{ health[row.id].warning }} 警告
          </el-tag>
          <el-tag v-else-if="health[row.id].total" size="small" type="success">正常</el-tag>
          <span v-else class="unset">无平台资源</span>
        </template>
      </el-table-column>
      <el-table-column prop="description" label="说明" min-width="140" show-overflow-tooltip>
        <template #default="{ row }">
          <span v-if="row.description">{{ row.description }}</span>
          <span v-else class="unset">—</span>
        </template>
      </el-table-column>
      <el-table-column prop="created_by_name" label="创建人" width="100">
        <template #default="{ row }">
          <span v-if="row.created_by_name">{{ row.created_by_name }}</span>
          <span v-else class="unset">—</span>
        </template>
      </el-table-column>
      <el-table-column prop="updated_at" label="更新时间" width="170">
        <template #default="{ row }">{{ formatTime(row.updated_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="220" fixed="right">
        <template #default="{ row }">
          <el-button size="small" type="primary" link @click="openEditor(row)">打开画布</el-button>
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

  <!-- 新建/编辑弹窗 -->
  <el-dialog v-model="dialogVisible" :title="editRow ? '编辑画布' : '新建画布'" width="560px">
    <el-form ref="formRef" :model="form" :rules="rules" label-width="100px">
      <el-form-item label="画布名称" prop="name">
        <el-input v-model="form.name" placeholder="如 加速链路" />
      </el-form-item>
      <el-form-item label="画布标识" prop="slug">
        <el-input v-model="form.slug" placeholder="如 ingress-path" :disabled="!!editRow" />
        <div class="tip">同一「项目组/渠道/环境/地域」下用标识区分多张图，建后不可改</div>
      </el-form-item>
      <el-form-item label="项目组" prop="project_group">
        <el-select v-model="form.project_group" style="width:100%" :disabled="!!editRow">
          <el-option v-for="i in dicts.projectGroups" :key="i.id" :label="i.name" :value="i.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="渠道" prop="channel">
        <el-select v-model="form.channel" style="width:100%" :disabled="!!editRow">
          <el-option v-for="i in dicts.channels" :key="i.id" :label="i.name" :value="i.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="环境" prop="environment">
        <el-select v-model="form.environment" style="width:100%" :disabled="!!editRow">
          <el-option v-for="i in dicts.environments" :key="i.id" :label="i.name" :value="i.id">
            <span>{{ i.name }}</span>
            <el-tag v-if="i.is_prod" size="small" type="danger" style="margin-left:6px">生产</el-tag>
          </el-option>
        </el-select>
      </el-form-item>
      <el-form-item label="地域">
        <el-input v-model="form.region" placeholder="留空表示跨地域，如 us / ap-southeast-1" :disabled="!!editRow" />
        <div class="tip">与服务器台账的「地域/机房」同口径，填了则自动编排只拉该地域主机</div>
      </el-form-item>
      <el-form-item label="说明">
        <el-input v-model="form.description" type="textarea" :rows="2" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="dialogVisible = false">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="handleSubmit">确定</el-button>
    </template>
  </el-dialog>

  <!-- 导入弹窗：选文件或直接粘贴导出的 JSON -->
  <el-dialog v-model="importVisible" title="导入画布" width="600px">
    <el-upload
      :auto-upload="false"
      :show-file-list="false"
      accept=".json,application/json"
      :on-change="handleFilePick"
    >
      <el-button :icon="Upload">选择导出的 .json 文件</el-button>
    </el-upload>
    <el-input
      v-model="importForm.text"
      type="textarea"
      :rows="6"
      placeholder="也可直接粘贴导出内容"
      style="margin: 10px 0"
    />
    <el-form label-width="120px">
      <el-form-item label="画布名称">
        <el-input v-model="importForm.name" placeholder="留空 = 用文件里的名称" />
      </el-form-item>
      <el-form-item label="画布标识">
        <el-input v-model="importForm.slug" placeholder="留空 = 用文件里的标识" />
      </el-form-item>
      <el-form-item label="项目组">
        <el-select v-model="importForm.project_group" clearable style="width:100%"
                   placeholder="留空 = 按文件里的编码匹配">
          <el-option v-for="i in dicts.projectGroups" :key="i.id" :label="i.name" :value="i.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="渠道">
        <el-select v-model="importForm.channel" clearable style="width:100%"
                   placeholder="留空 = 按文件里的编码匹配">
          <el-option v-for="i in dicts.channels" :key="i.id" :label="i.name" :value="i.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="环境">
        <el-select v-model="importForm.environment" clearable style="width:100%"
                   placeholder="留空 = 按文件里的编码匹配">
          <el-option v-for="i in dicts.environments" :key="i.id" :label="i.name" :value="i.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="覆盖已存在">
        <el-switch v-model="importForm.overwrite" :disabled="!canWrite" />
        <span class="tip">
          同维度同标识的画布已存在时整份覆盖它（需读写权限）；关闭则直接报冲突
        </span>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="importVisible = false">取消</el-button>
      <el-button type="primary" :loading="importing" @click="submitImport">导入</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { ref, computed, reactive, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { Plus, Edit, Delete, Upload } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { debounce } from '../../utils/debounce'
import { useAuthStore } from '../../stores/auth'
import {
  projectGroupApi, channelApi, environmentApi, fetchDictOptions,
} from '../../api/assets'
import {
  getCanvases, createCanvas, updateCanvas, deleteCanvas, importCanvas, getTopoHealth,
} from '../../api/topo'

const router = useRouter()
const authStore = useAuthStore()
const canCreate = computed(() => authStore.can('topo', 'create'))
const canWrite = computed(() => authStore.can('topo', 'write'))

const loading = ref(false)
const submitting = ref(false)
const tableData = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const filter = reactive({ keyword: '', project_group: '', channel: '', env: '' })
const dicts = reactive({ projectGroups: [], channels: [], environments: [] })
// 画布 id → 健康度汇总。单独一发请求：汇总要遍历所有引用型节点，绑进列表会让翻页
// 也付一次这个代价；慢了最多是那一列晚出来，不拖住表格
const health = ref({})
const dialogVisible = ref(false)
const editRow = ref(null)
const formRef = ref()

const emptyForm = () => ({
  name: '', slug: '', project_group: null, channel: null, environment: null,
  region: '', description: '',
})
const form = reactive(emptyForm())
const rules = {
  name: [{ required: true, message: '请输入画布名称' }],
  slug: [
    { required: true, message: '请输入画布标识' },
    { pattern: /^[a-z0-9]([a-z0-9-]*[a-z0-9])?$/, message: '只能用小写字母、数字与连字符' },
  ],
  project_group: [{ required: true, message: '请选择项目组' }],
  channel: [{ required: true, message: '请选择渠道' }],
  environment: [{ required: true, message: '请选择环境' }],
}

function handleSearch() {
  searchDebounced.cancel()
  page.value = 1
  fetchData()
}
const searchDebounced = debounce(handleSearch)
onUnmounted(() => searchDebounced.cancel())

function formatTime(value) {
  return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—'
}

async function fetchData() {
  loading.value = true
  try {
    const params = { page: page.value, page_size: pageSize.value }
    for (const [key, value] of Object.entries(filter)) {
      if (value) params[key] = value
    }
    const res = await getCanvases(params)
    tableData.value = res.data?.results || []
    total.value = res.data?.total || 0
  } finally {
    loading.value = false
  }
  fetchHealth()
}

async function fetchHealth() {
  try {
    const res = await getTopoHealth()
    health.value = Object.fromEntries(
      (res.data?.results || []).map((item) => [item.canvas.id, item]))
  } catch {
    // 健康度拉不到不影响列表本身：这一列留占位就是了
  }
}

function openEditor(row) {
  router.push({ name: 'TopoCanvasEditor', params: { id: row.id } })
}

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
      // 维度与标识建后不可改：它们是画布的身份，改了等于换成另一张图，
      // 而图上的节点是按原维度挑出来的，跟着换会得到一张自相矛盾的图
      await updateCanvas(editRow.value.id, {
        name: form.name, description: form.description,
      })
      ElMessage.success('更新成功')
    } else {
      await createCanvas(form)
      ElMessage.success('新建成功')
    }
    dialogVisible.value = false
    fetchData()
  } finally {
    submitting.value = false
  }
}

// ── 导入 ────────────────────────────────────────────────

const importVisible = ref(false)
const importing = ref(false)
const importForm = reactive({
  text: '', name: '', slug: '',
  project_group: null, channel: null, environment: null, overwrite: false,
})

function openImport() {
  Object.assign(importForm, {
    text: '', name: '', slug: '',
    project_group: null, channel: null, environment: null, overwrite: false,
  })
  importVisible.value = true
}

// 只读文本，不上传文件：后端导入接口收的是 JSON 体，走 multipart 反而多一层解析
function handleFilePick(file) {
  const raw = file.raw
  if (!raw) return
  const reader = new FileReader()
  reader.onload = () => { importForm.text = String(reader.result || '') }
  reader.onerror = () => ElMessage.error('文件读取失败')
  reader.readAsText(raw)
}

async function submitImport() {
  if (!importForm.text.trim()) {
    ElMessage.warning('请先选择文件或粘贴导出内容')
    return
  }
  let payload
  try {
    payload = JSON.parse(importForm.text)
  } catch {
    ElMessage.error('内容不是合法的 JSON')
    return
  }
  const overrides = {}
  for (const key of ['name', 'slug', 'project_group', 'channel', 'environment']) {
    if (importForm[key]) overrides[key] = importForm[key]
  }
  importing.value = true
  try {
    const res = await importCanvas({
      payload, overrides, overwrite: importForm.overwrite,
    })
    importVisible.value = false
    ElMessage.success(res.msg || '导入完成')
    // 跳过的节点逐条列出来：只报个数量，人不知道要补哪些
    const skipped = res.data?.report?.skipped_nodes || []
    if (skipped.length) {
      ElMessageBox.alert(
        skipped.map((s) => `${s.label}（${s.reason}）`).join('\n'),
        `有 ${skipped.length} 个节点没有落地`,
        { type: 'warning', customStyle: { whiteSpace: 'pre-line' } },
      )
    }
    fetchData()
  } finally {
    importing.value = false
  }
}

async function handleDelete(row) {
  await ElMessageBox.confirm(
    `确定删除画布「${row.name}」吗？图上的 ${row.node_count} 个节点与 ${row.edge_count} 条连线会一并删除，` +
    '被引用的服务器、域名与证书本身不受影响。',
    '警告',
    { type: 'warning' },
  )
  await deleteCanvas(row.id)
  ElMessage.success('删除成功')
  fetchData()
}

onMounted(async () => {
  fetchData()
  const [groups, channels, envs] = await Promise.all([
    fetchDictOptions(projectGroupApi),
    fetchDictOptions(channelApi),
    fetchDictOptions(environmentApi),
  ])
  dicts.projectGroups = groups.data?.results || []
  dicts.channels = channels.data?.results || []
  dicts.environments = envs.data?.results || []
})
</script>

<style scoped>
.toolbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.toolbar-left, .toolbar-right { display: flex; gap: 8px; }
.mono { font-family: monospace; }
.region { margin-left: 6px; font-size: 12px; color: #606266; }
.unset, .muted { color: #909399; font-size: 12px; }
.tip { font-size: 12px; color: #909399; line-height: 1.6; }
</style>
