<template>
  <el-card shadow="never">
    <!-- 工具栏 -->
    <div class="toolbar">
      <div class="toolbar-left">
        <el-input
          v-model="filter.keyword"
          placeholder="用户名 / 邮箱 / 手机号"
          clearable
          style="width: 220px"
          :prefix-icon="Search"
          @input="searchDebounced"
          @keyup.enter="handleSearch"
          @clear="handleSearch"
        />
        <el-select v-model="filter.is_active" placeholder="状态" clearable style="width: 120px" @change="handleSearch">
          <el-option label="启用" value="true" />
          <el-option label="停用" value="false" />
        </el-select>
        <el-button :icon="Search" @click="handleSearch">查询</el-button>
      </div>
      <el-button type="primary" :icon="Plus" @click="openDialog()">新增用户</el-button>
    </div>

    <!-- 表格 -->
    <el-table v-loading="loading" :data="tableData" stripe>
      <el-table-column prop="username" label="用户名" min-width="120" />
      <el-table-column prop="email" label="邮箱" min-width="160" show-overflow-tooltip />
      <el-table-column prop="phone" label="手机号" width="130" />
      <el-table-column prop="department" label="部门" width="120" />
      <el-table-column label="角色" width="90">
        <template #default="{ row }">
          <el-tag size="small" :type="row.is_staff ? 'danger' : 'info'">
            {{ row.is_staff ? '管理员' : '普通用户' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="80">
        <template #default="{ row }">
          <el-tag size="small" :type="row.is_active ? 'success' : 'info'">
            {{ row.is_active ? '启用' : '停用' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="MFA" width="90">
        <template #default="{ row }">
          <el-tag size="small" :type="row.mfa_enabled ? 'success' : 'info'">
            {{ row.mfa_enabled ? '已绑定' : '未绑定' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="最后登录" width="150">
        <template #default="{ row }">{{ formatTime(row.last_login) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="340" fixed="right">
        <template #default="{ row }">
          <el-button size="small" :icon="Edit" @click="openDialog(row)">编辑</el-button>
          <el-button size="small" :icon="Key" @click="openResetDialog(row)">重置密码</el-button>
          <el-button
            v-if="row.mfa_enabled"
            size="small"
            @click="handleResetMfa(row)"
          >重置 MFA</el-button>
          <el-button
            size="small"
            :type="row.is_active ? 'warning' : 'success'"
            @click="handleToggle(row)"
          >{{ row.is_active ? '停用' : '启用' }}</el-button>
          <el-button size="small" type="danger" :icon="Delete" @click="handleDelete(row)" />
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
  <el-dialog v-model="dialogVisible" :title="editRow ? '编辑用户' : '新增用户'" width="520px">
    <el-form ref="formRef" :model="form" :rules="rules" label-width="90px">
      <el-form-item label="用户名" prop="username">
        <el-input v-model="form.username" :disabled="!!editRow" />
      </el-form-item>
      <el-form-item label="密码" :prop="editRow ? '' : 'password'">
        <el-input
          v-model="form.password"
          type="password"
          show-password
          :placeholder="editRow ? '留空则不修改' : '请输入初始密码'"
        />
      </el-form-item>
      <el-form-item label="邮箱">
        <el-input v-model="form.email" />
      </el-form-item>
      <el-form-item label="手机号">
        <el-input v-model="form.phone" />
      </el-form-item>
      <el-form-item label="部门">
        <el-input v-model="form.department" />
      </el-form-item>
      <el-form-item label="管理员">
        <el-switch v-model="form.is_staff" />
      </el-form-item>
      <el-form-item label="状态">
        <el-switch v-model="form.is_active" active-text="启用" inactive-text="停用" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="dialogVisible = false">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="handleSubmit">确定</el-button>
    </template>
  </el-dialog>

  <!-- 重置密码弹窗 -->
  <el-dialog v-model="resetVisible" title="重置密码" width="420px">
    <el-form ref="resetFormRef" :model="resetForm" :rules="resetRules" label-width="90px">
      <el-form-item label="新密码" prop="new_password">
        <el-input v-model="resetForm.new_password" type="password" show-password />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="resetVisible = false">取消</el-button>
      <el-button type="primary" :loading="resetSubmitting" @click="handleReset">确定</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { ref, reactive, onMounted, onUnmounted } from 'vue'
import { debounce } from '../../utils/debounce'
import { Plus, Edit, Delete, Search, Key } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  getUsers, createUser, updateUser, deleteUser,
  toggleUserActive, resetUserPassword, resetUserMfa,
} from '../../api/users'

const loading = ref(false)
const submitting = ref(false)
const tableData = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const filter = reactive({ keyword: '', is_active: '' })

const dialogVisible = ref(false)
const editRow = ref(null)
const formRef = ref()
const emptyForm = () => ({
  username: '', password: '', email: '', phone: '',
  department: '', is_staff: false, is_active: true,
})
const form = reactive(emptyForm())
const rules = {
  username: [{ required: true, message: '请输入用户名' }],
  password: [{ required: true, message: '请输入初始密码' }, { min: 8, message: '至少 8 个字符' }],
}

const resetVisible = ref(false)
const resetSubmitting = ref(false)
const resetRow = ref(null)
const resetFormRef = ref()
const resetForm = reactive({ new_password: '' })
const resetRules = {
  new_password: [{ required: true, message: '请输入新密码' }, { min: 8, message: '至少 8 个字符' }],
}

const formatTime = (t) => (t ? t.slice(0, 19).replace('T', ' ') : '从未登录')

async function fetchData() {
  loading.value = true
  try {
    const params = { page: page.value, page_size: pageSize.value }
    if (filter.keyword) params.keyword = filter.keyword
    if (filter.is_active) params.is_active = filter.is_active
    const res = await getUsers(params)
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

function openDialog(row = null) {
  editRow.value = row
  Object.assign(form, row ? { ...emptyForm(), ...row, password: '' } : emptyForm())
  dialogVisible.value = true
  formRef.value?.clearValidate()
}

async function handleSubmit() {
  await formRef.value.validate()
  submitting.value = true
  try {
    const payload = { ...form }
    if (editRow.value && !payload.password) delete payload.password
    if (editRow.value) {
      await updateUser(editRow.value.id, payload)
      ElMessage.success('更新成功')
    } else {
      await createUser(payload)
      ElMessage.success('新增成功')
    }
    dialogVisible.value = false
    fetchData()
  } finally {
    submitting.value = false
  }
}

function openResetDialog(row) {
  resetRow.value = row
  resetForm.new_password = ''
  resetVisible.value = true
  resetFormRef.value?.clearValidate()
}

async function handleReset() {
  await resetFormRef.value.validate()
  resetSubmitting.value = true
  try {
    await resetUserPassword(resetRow.value.id, { new_password: resetForm.new_password })
    ElMessage.success('密码已重置')
    resetVisible.value = false
  } finally {
    resetSubmitting.value = false
  }
}

async function handleToggle(row) {
  await toggleUserActive(row.id)
  ElMessage.success(row.is_active ? '已停用' : '已启用')
  fetchData()
}

async function handleResetMfa(row) {
  // 丢手机时的兜底：清掉绑定，用户下次登录重新绑（仍需密码，不构成越权）
  await ElMessageBox.confirm(
    `确定清除用户「${row.username}」的 MFA 绑定吗？该用户下次登录需重新绑定。`,
    '重置二次认证', { type: 'warning' },
  )
  await resetUserMfa(row.id)
  ElMessage.success('已清除 MFA 绑定')
  fetchData()
}

async function handleDelete(row) {
  await ElMessageBox.confirm(`确定删除用户「${row.username}」吗？`, '警告', { type: 'warning' })
  await deleteUser(row.id)
  ElMessage.success('删除成功')
  fetchData()
}

onMounted(fetchData)
</script>

<style scoped>
.toolbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.toolbar-left { display: flex; gap: 8px; }
</style>
