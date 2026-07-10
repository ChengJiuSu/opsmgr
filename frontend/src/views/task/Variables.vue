<template>
  <el-card shadow="never">
    <div class="toolbar">
      <div class="toolbar-left">
        <el-input v-model="filter.keyword" placeholder="变量名 / 说明" clearable style="width:180px" @keyup.enter="handleSearch" @clear="handleSearch" />
        <el-select v-model="filter.enabled" placeholder="状态" clearable style="width:110px" @change="handleSearch">
          <el-option label="启用" value="true" />
          <el-option label="停用" value="false" />
        </el-select>
        <el-button :icon="Search" @click="handleSearch">查询</el-button>
        <el-button :icon="Refresh" @click="fetchData">刷新</el-button>
      </div>
      <el-button v-if="authStore.isStaff" type="primary" :icon="Plus" @click="openDialog()">新增变量</el-button>
    </div>

    <el-alert type="info" :closable="false" style="margin-bottom: 12px">
      启用中的变量在任务执行时以 <code>--extra-vars</code> 注入全部模块（命令内可用
      <code v-pre>{{ 变量名 }}</code> 引用），模块自身的额外变量覆盖同名项；本列表 API
      亦可供 Jenkins 等外部系统拉取变量后自行调用 ansible（规划中）。
    </el-alert>

    <el-table v-loading="loading" :data="tableData" stripe>
      <el-table-column prop="key" label="变量名" min-width="140" show-overflow-tooltip>
        <template #default="{ row }"><code>{{ row.key }}</code></template>
      </el-table-column>
      <el-table-column prop="value" label="变量值" min-width="200" show-overflow-tooltip />
      <el-table-column prop="description" label="说明" min-width="160" show-overflow-tooltip>
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
      <el-table-column v-if="authStore.isStaff" label="操作" width="150" fixed="right">
        <template #default="{ row }">
          <el-button size="small" :icon="EditPen" @click="openDialog(row)">编辑</el-button>
          <el-button size="small" type="danger" :icon="Delete" @click="handleDelete(row)" />
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

  <!-- 新增 / 编辑弹窗 -->
  <el-dialog v-model="dialogVisible" :title="form.id ? '编辑变量' : '新增变量'" width="520px">
    <el-form ref="formRef" :model="form" :rules="rules" label-width="80px">
      <el-form-item label="变量名" prop="key">
        <el-input v-model="form.key" placeholder="如 app_env（字母/下划线开头，仅字母数字下划线）" />
      </el-form-item>
      <el-form-item label="变量值" prop="value">
        <el-input v-model="form.value" type="textarea" :rows="3" placeholder="任意文本，剧本/命令中以 Jinja 变量引用" />
      </el-form-item>
      <el-form-item label="说明">
        <el-input v-model="form.description" placeholder="用途说明，可留空" />
      </el-form-item>
      <el-form-item label="启用">
        <el-switch v-model="form.enabled" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="dialogVisible = false">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="handleSubmit">保存</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { Plus, Delete, Search, Refresh, EditPen } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useAuthStore } from '../../stores/auth'
import { getVariables, createVariable, updateVariable, deleteVariable } from '../../api/tasks'

const authStore = useAuthStore()
const loading = ref(false)
const submitting = ref(false)
const tableData = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const filter = reactive({ keyword: '', enabled: '' })

const formatTime = (t) => (t ? t.slice(0, 19).replace('T', ' ') : '')

const dialogVisible = ref(false)
const formRef = ref()
const emptyForm = () => ({ id: null, key: '', value: '', description: '', enabled: true })
const form = reactive(emptyForm())
const rules = {
  key: [
    { required: true, message: '请输入变量名' },
    { pattern: /^[a-zA-Z_][a-zA-Z0-9_]*$/, message: '须以字母/下划线开头，仅含字母、数字、下划线' },
  ],
}

async function fetchData() {
  loading.value = true
  try {
    const params = { page: page.value, page_size: pageSize.value }
    if (filter.keyword) params.keyword = filter.keyword
    if (filter.enabled) params.enabled = filter.enabled
    const res = await getVariables(params)
    tableData.value = res.data?.results || []
    total.value = res.data?.total || 0
  } finally {
    loading.value = false
  }
}

function handleSearch() {
  page.value = 1
  fetchData()
}

function openDialog(row) {
  Object.assign(form, emptyForm(), row ? {
    id: row.id, key: row.key, value: row.value,
    description: row.description, enabled: row.enabled,
  } : {})
  dialogVisible.value = true
  formRef.value?.clearValidate()
}

async function handleSubmit() {
  await formRef.value.validate()
  submitting.value = true
  try {
    const payload = { key: form.key, value: form.value, description: form.description, enabled: form.enabled }
    if (form.id) {
      await updateVariable(form.id, payload)
      ElMessage.success('变量已保存')
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
    `确定删除变量「${row.key}」吗？执行中引用该变量的命令/剧本将取不到值。`, '警告', { type: 'warning' },
  )
  await deleteVariable(row.id)
  ElMessage.success('删除成功')
  fetchData()
}

onMounted(fetchData)
</script>

<style scoped>
.toolbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.toolbar-left { display: flex; gap: 8px; flex-wrap: wrap; }
.unset { color: #c0c4cc; }
code { background: #f5f7fa; padding: 1px 6px; border-radius: 3px; font-size: 12px; }
</style>
