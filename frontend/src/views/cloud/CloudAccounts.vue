<template>
  <el-card shadow="never">
    <!-- 工具栏 -->
    <div class="toolbar">
      <div class="toolbar-left">
        <el-select v-model="filter.provider" placeholder="云厂商" clearable style="width:160px" @change="fetchData">
          <el-option v-for="p in providerOptions" :key="p.id" :label="p.name" :value="p.code" />
        </el-select>
        <el-select v-model="filter.enabled" placeholder="启用状态" clearable style="width:120px" @change="fetchData">
          <el-option label="启用" value="true" />
          <el-option label="停用" value="false" />
        </el-select>
      </div>
      <el-button type="primary" :icon="Plus" @click="openDialog()">新增云账号</el-button>
    </div>

    <!-- 表格 -->
    <el-table v-loading="loading" :data="tableData" stripe>
      <el-table-column prop="name" label="账号名称" min-width="140" />
      <el-table-column prop="provider_name" label="云厂商" min-width="100">
        <template #default="{ row }">
          <el-tag size="small" type="info">{{ row.provider_name }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="access_key" label="AccessKey ID" min-width="180">
        <template #default="{ row }">
          <span class="mono">{{ maskKey(row.access_key) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="Secret" width="90">
        <template #default="{ row }">
          <el-tag size="small" :type="row.has_secret_key ? 'success' : 'danger'">
            {{ row.has_secret_key ? '已设置' : '未设置' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="domain_count" label="域名数" width="80" />
      <el-table-column label="启用" width="80">
        <template #default="{ row }">
          <el-tag size="small" :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '启用' : '停用' }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="remark" label="备注" min-width="120" show-overflow-tooltip />
      <el-table-column label="操作" width="240" fixed="right">
        <template #default="{ row }">
          <el-button size="small" :loading="testingId === row.id" @click="handleTest(row)">测试</el-button>
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
  <el-dialog v-model="dialogVisible" :title="editRow ? '编辑云账号' : '新增云账号'" width="520px">
    <el-form ref="formRef" :model="form" :rules="rules" label-width="130px">
      <el-form-item label="账号名称" prop="name">
        <el-input v-model="form.name" placeholder="如 阿里云-主账号" />
      </el-form-item>
      <el-form-item label="云厂商" prop="provider">
        <el-select v-model="form.provider" style="width:100%">
          <el-option v-for="p in providerOptions" :key="p.id" :label="p.name" :value="p.id" />
        </el-select>
        <div class="tip">DNS 管理当前支持阿里云（aliyun）、腾讯云（tencent）</div>
      </el-form-item>
      <el-form-item label="AccessKey ID" prop="access_key">
        <el-input v-model="form.access_key" />
      </el-form-item>
      <el-form-item label="AccessKey Secret" :prop="editRow ? '' : 'secret_key'">
        <el-input
          v-model="form.secret_key"
          type="password"
          show-password
          :placeholder="editRow ? '留空表示不修改' : ''"
        />
      </el-form-item>
      <el-form-item label="启用">
        <el-switch v-model="form.enabled" />
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
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { Plus, Edit, Delete } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { cloudProviderApi, fetchDictOptions } from '../../api/assets'
import {
  getCloudAccounts, createCloudAccount, updateCloudAccount,
  deleteCloudAccount, testCloudAccount,
} from '../../api/cloud'

const loading = ref(false)
const submitting = ref(false)
const testingId = ref(null)
const tableData = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const filter = reactive({ provider: '', enabled: '' })
const providerOptions = ref([])
const dialogVisible = ref(false)
const editRow = ref(null)
const formRef = ref()

const emptyForm = () => ({
  name: '', provider: null, access_key: '', secret_key: '', enabled: true, remark: '',
})
const form = reactive(emptyForm())
const rules = {
  name: [{ required: true, message: '请输入账号名称' }],
  provider: [{ required: true, message: '请选择云厂商' }],
  access_key: [{ required: true, message: '请输入 AccessKey ID' }],
  secret_key: [{ required: true, message: '请输入 AccessKey Secret' }],
}

// AK 掩码展示：保留前 4 后 4
function maskKey(key) {
  if (!key || key.length <= 8) return key
  return `${key.slice(0, 4)}****${key.slice(-4)}`
}

async function fetchData() {
  loading.value = true
  try {
    const params = { page: page.value, page_size: pageSize.value }
    if (filter.provider) params.provider = filter.provider
    if (filter.enabled) params.enabled = filter.enabled
    const res = await getCloudAccounts(params)
    tableData.value = res.data?.results || []
    total.value = res.data?.total || 0
  } finally {
    loading.value = false
  }
}

function openDialog(row = null) {
  editRow.value = row
  Object.assign(form, row ? { ...row, secret_key: '' } : emptyForm())
  dialogVisible.value = true
  formRef.value?.clearValidate()
}

async function handleSubmit() {
  await formRef.value.validate()
  submitting.value = true
  try {
    if (editRow.value) {
      await updateCloudAccount(editRow.value.id, form)
      ElMessage.success('更新成功')
    } else {
      await createCloudAccount(form)
      ElMessage.success('新增成功')
    }
    dialogVisible.value = false
    fetchData()
  } finally {
    submitting.value = false
  }
}

// 凭据连通性测试：成功时提示账号下托管域名数量
async function handleTest(row) {
  testingId.value = row.id
  try {
    const res = await testCloudAccount(row.id)
    ElMessage.success(res.msg || '连接成功')
  } finally {
    testingId.value = null
  }
}

async function handleDelete(row) {
  await ElMessageBox.confirm(
    `确定删除云账号「${row.name}」吗？账号下存在主域名时将无法删除。`,
    '警告',
    { type: 'warning' },
  )
  await deleteCloudAccount(row.id)
  ElMessage.success('删除成功')
  fetchData()
}

onMounted(async () => {
  fetchData()
  const res = await fetchDictOptions(cloudProviderApi)
  providerOptions.value = res.data?.results || []
})
</script>

<style scoped>
.toolbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.toolbar-left { display: flex; gap: 8px; }
.mono { font-family: monospace; }
.tip { font-size: 12px; color: #909399; line-height: 1.6; }
</style>
