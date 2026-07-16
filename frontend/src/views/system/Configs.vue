<template>
  <el-card shadow="never">
    <div class="toolbar">
      <el-input
        v-model="keyword"
        placeholder="搜索配置项 / 说明"
        clearable
        :prefix-icon="Search"
        style="width:240px"
      />
      <el-button type="primary" :icon="Plus" @click="openDialog()">新增配置</el-button>
    </div>

    <!-- 树形表格：按配置项前缀分组（jenkins.url → Jenkins 组），无前缀归「通用配置」 -->
    <el-table
      v-loading="loading"
      :data="treeData"
      row-key="rowId"
      default-expand-all
      :tree-props="{ children: 'children' }"
    >
      <el-table-column label="配置项" min-width="240">
        <template #default="{ row }">
          <template v-if="row.isGroup">
            <span class="group-name">{{ row.label }}</span>
            <el-tag size="small" type="info" style="margin-left:8px">{{ row.children.length }} 项</el-tag>
          </template>
          <code v-else class="key-code">{{ row.key }}</code>
        </template>
      </el-table-column>
      <el-table-column label="配置值" show-overflow-tooltip>
        <template #default="{ row }">
          <span v-if="!row.isGroup">{{ maskValue(row) }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="description" label="说明" show-overflow-tooltip>
        <template #default="{ row }">
          <span v-if="!row.isGroup">{{ row.description }}</span>
        </template>
      </el-table-column>
      <el-table-column label="更新时间" width="165">
        <template #default="{ row }">
          <span v-if="!row.isGroup">{{ formatTime(row.updated_at) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="170" fixed="right">
        <template #default="{ row }">
          <el-button v-if="row.isGroup" size="small" :icon="Plus" link type="primary" @click="openDialog(null, row.prefix)">
            新增本组配置
          </el-button>
          <template v-else>
            <el-button size="small" :icon="Edit" @click="openDialog(row)">编辑</el-button>
            <el-button size="small" type="danger" :icon="Delete" @click="handleDelete(row)">删除</el-button>
          </template>
        </template>
      </el-table-column>
    </el-table>
  </el-card>

  <el-dialog v-model="dialogVisible" :title="editRow ? '编辑配置' : '新增配置'" width="480px">
    <el-form ref="formRef" :model="form" :rules="rules" label-width="80px">
      <el-form-item label="上层分组" prop="group">
        <el-select
          v-model="form.group"
          filterable
          allow-create
          default-first-option
          :disabled="!!editRow"
          style="width:100%"
          placeholder="选择已有分组或输入新分组"
        >
          <el-option :label="`${GENERAL_LABEL}（无前缀）`" :value="GENERAL" />
          <el-option v-for="g in groupOptions" :key="g" :label="GROUP_LABELS[g] || g" :value="g" />
        </el-select>
      </el-form-item>
      <el-form-item label="配置名" prop="name">
        <el-input v-model="form.name" :disabled="!!editRow" placeholder="如 url、api_token" />
        <div class="form-tip">
          最终配置项：<code>{{ finalKey || '—' }}</code>
        </div>
      </el-form-item>
      <el-form-item label="配置值" prop="value">
        <el-input v-model="form.value" type="textarea" :rows="3" />
      </el-form-item>
      <el-form-item label="说明">
        <el-input v-model="form.description" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="dialogVisible = false">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="handleSubmit">确定</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { Plus, Edit, Delete, Search } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { getConfigs, createConfig, updateConfig, deleteConfig } from '../../api/system'

// 无前缀配置的分组哨兵与展示名
const GENERAL = '__general__'
const GENERAL_LABEL = '通用配置'
// 已知分组的展示名（其余前缀原样展示）
const GROUP_LABELS = {
  jenkins: 'Jenkins',
  jumpserver: 'JumpServer',
  artifact: '制品管理',
}
// 值中含这些片段的配置项视为敏感，列表中打码显示
const SENSITIVE_KEY_HINTS = ['token', 'secret', 'password', 'api_key']

const loading = ref(false)
const submitting = ref(false)
const allConfigs = ref([])
const keyword = ref('')
const dialogVisible = ref(false)
const editRow = ref(null)
const formRef = ref()

const emptyForm = () => ({ group: GENERAL, name: '', value: '', description: '' })
const form = reactive(emptyForm())
const rules = {
  name: [
    { required: true, message: '请输入配置名' },
    { pattern: /^[A-Za-z0-9_.-]+$/, message: '仅允许字母/数字/下划线/点/横线' },
  ],
  value: [{ required: true, message: '请输入配置值' }],
}

const formatTime = (t) => (t ? t.replace('T', ' ').slice(0, 19) : '')

// 敏感值打码：保留前 3 位便于确认配置对象
function maskValue(row) {
  const lower = row.key.toLowerCase()
  if (SENSITIVE_KEY_HINTS.some((hint) => lower.includes(hint)) && row.value) {
    return `${row.value.slice(0, 3)}******`
  }
  return row.value
}

const prefixOf = (key) => {
  const dot = key.indexOf('.')
  return dot > 0 ? key.slice(0, dot) : GENERAL
}

// 现有分组前缀（新增弹窗下拉候选）
const groupOptions = computed(() =>
  [...new Set(allConfigs.value.map((c) => prefixOf(c.key)))]
    .filter((g) => g !== GENERAL)
    .sort(),
)

// 按前缀分组构造树：分组行 + 子配置行；关键字过滤命中 key/说明
const treeData = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  const rows = allConfigs.value.filter(
    (c) => !kw || c.key.toLowerCase().includes(kw) || (c.description || '').toLowerCase().includes(kw),
  )
  const groups = new Map()
  for (const row of rows) {
    const prefix = prefixOf(row.key)
    if (!groups.has(prefix)) groups.set(prefix, [])
    groups.get(prefix).push({ ...row, rowId: `cfg-${row.id}` })
  }
  return [...groups.entries()]
    .sort(([a], [b]) => (a === GENERAL) - (b === GENERAL) || a.localeCompare(b)) // 通用组排最后
    .map(([prefix, children]) => ({
      rowId: `group-${prefix}`,
      isGroup: true,
      prefix,
      label: prefix === GENERAL ? GENERAL_LABEL : (GROUP_LABELS[prefix] || prefix),
      children: children.sort((x, y) => x.key.localeCompare(y.key)),
    }))
})

// 弹窗内实时预览最终 key
const finalKey = computed(() => {
  if (!form.name) return ''
  return form.group === GENERAL ? form.name : `${form.group}.${form.name}`
})

async function fetchData() {
  loading.value = true
  try {
    // 配置项数量有限，一次拉全量（page_size=0 返回全部），分组在前端完成
    const res = await getConfigs({ page_size: 0 })
    allConfigs.value = res.data?.results || []
  } finally {
    loading.value = false
  }
}

function openDialog(row = null, prefix = null) {
  editRow.value = row
  Object.assign(form, emptyForm())
  if (row) {
    // 编辑：按首个点拆回 分组 + 配置名（key 不可改，仅改值与说明）
    const dot = row.key.indexOf('.')
    form.group = dot > 0 ? row.key.slice(0, dot) : GENERAL
    form.name = dot > 0 ? row.key.slice(dot + 1) : row.key
    form.value = row.value
    form.description = row.description
  } else if (prefix && prefix !== GENERAL) {
    form.group = prefix
  }
  dialogVisible.value = true
  formRef.value?.clearValidate()
}

async function handleSubmit() {
  await formRef.value.validate()
  submitting.value = true
  try {
    const payload = { key: finalKey.value, value: form.value, description: form.description }
    if (editRow.value) {
      await updateConfig(editRow.value.id, payload)
      ElMessage.success('更新成功')
    } else {
      await createConfig(payload)
      ElMessage.success('新增成功')
    }
    dialogVisible.value = false
    fetchData()
  } finally {
    submitting.value = false
  }
}

async function handleDelete(row) {
  await ElMessageBox.confirm(`确定删除配置项「${row.key}」吗？`, '警告', { type: 'warning' })
  await deleteConfig(row.id)
  ElMessage.success('删除成功')
  fetchData()
}

onMounted(fetchData)
</script>

<style scoped>
.toolbar { display: flex; justify-content: space-between; margin-bottom: 16px; }
.group-name { font-weight: 600; color: #303133; }
.key-code { background: #f5f7fa; padding: 1px 8px; border-radius: 3px; font-size: 12px; }
.form-tip { color: #909399; font-size: 12px; line-height: 1.5; margin-top: 4px; }
.form-tip code { background: #f5f7fa; padding: 1px 6px; border-radius: 3px; }
</style>
