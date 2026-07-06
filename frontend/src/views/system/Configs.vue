<template>
  <el-card shadow="never">
    <div class="toolbar">
      <span />
      <el-button type="primary" :icon="Plus" @click="openDialog()">新增配置</el-button>
    </div>

    <el-table v-loading="loading" :data="tableData" stripe>
      <el-table-column prop="key" label="配置项" width="200" />
      <el-table-column prop="value" label="配置值" show-overflow-tooltip />
      <el-table-column prop="description" label="说明" show-overflow-tooltip />
      <el-table-column prop="updated_at" label="更新时间" width="165">
        <template #default="{ row }">{{ formatTime(row.updated_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="140" fixed="right">
        <template #default="{ row }">
          <el-button size="small" :icon="Edit" @click="openDialog(row)">编辑</el-button>
          <el-button size="small" type="danger" :icon="Delete" @click="handleDelete(row)">删除</el-button>
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

  <el-dialog v-model="dialogVisible" :title="editRow ? '编辑配置' : '新增配置'" width="480px">
    <el-form ref="formRef" :model="form" :rules="rules" label-width="80px">
      <el-form-item label="配置项" prop="key">
        <el-input v-model="form.key" :disabled="!!editRow" placeholder="如 MAX_UPLOAD_SIZE" />
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
import { ref, reactive, onMounted } from 'vue'
import { Plus, Edit, Delete } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { getConfigs, createConfig, updateConfig, deleteConfig } from '../../api/system'

const loading = ref(false)
const submitting = ref(false)
const tableData = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const dialogVisible = ref(false)
const editRow = ref(null)
const formRef = ref()

const emptyForm = () => ({ key: '', value: '', description: '' })
const form = reactive(emptyForm())
const rules = {
  key: [{ required: true, message: '请输入配置项' }],
  value: [{ required: true, message: '请输入配置值' }],
}

const formatTime = (t) => t ? t.replace('T', ' ').slice(0, 19) : ''

async function fetchData() {
  loading.value = true
  try {
    const res = await getConfigs({ page: page.value, page_size: pageSize.value })
    tableData.value = res.data?.results || []
    total.value = res.data?.total || 0
  } finally {
    loading.value = false
  }
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
      await updateConfig(editRow.value.id, form)
      ElMessage.success('更新成功')
    } else {
      await createConfig(form)
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
</style>
