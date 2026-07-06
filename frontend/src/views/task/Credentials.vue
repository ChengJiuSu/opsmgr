<template>
  <el-card shadow="never">
    <div class="toolbar">
      <span class="hint">连接凭据用于 Ansible 登录目标主机。密码 / 私钥保存后不再回显。</span>
      <el-button type="primary" :icon="Plus" @click="openDialog()">新增凭据</el-button>
    </div>

    <el-table v-loading="loading" :data="tableData" stripe>
      <el-table-column prop="name" label="名称" min-width="120" />
      <el-table-column prop="username" label="登录用户" width="120" />
      <el-table-column label="认证方式" width="100">
        <template #default="{ row }">
          <el-tag size="small">{{ row.auth_type === 'key' ? '私钥' : '密码' }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="已设置凭据" width="120">
        <template #default="{ row }">
          <el-tag size="small" :type="(row.has_password || row.has_private_key) ? 'success' : 'info'">
            {{ (row.has_password || row.has_private_key) ? '是' : '否' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="提权" width="80">
        <template #default="{ row }">
          <el-tag size="small" :type="row.become ? 'warning' : 'info'">{{ row.become ? 'sudo' : '否' }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="updated_at" label="更新时间" width="160">
        <template #default="{ row }">{{ formatTime(row.updated_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="140" fixed="right">
        <template #default="{ row }">
          <el-button size="small" :icon="Edit" @click="openDialog(row)">编辑</el-button>
          <el-button size="small" type="danger" :icon="Delete" @click="handleDelete(row)" />
        </template>
      </el-table-column>
    </el-table>
  </el-card>

  <el-dialog v-model="dialogVisible" :title="editRow ? '编辑凭据' : '新增凭据'" width="520px">
    <el-form ref="formRef" :model="form" :rules="rules" label-width="100px">
      <el-form-item label="名称" prop="name">
        <el-input v-model="form.name" :disabled="!!editRow" />
      </el-form-item>
      <el-form-item label="登录用户" prop="username">
        <el-input v-model="form.username" />
      </el-form-item>
      <el-form-item label="认证方式">
        <el-radio-group v-model="form.auth_type">
          <el-radio-button value="password">密码</el-radio-button>
          <el-radio-button value="key">私钥</el-radio-button>
        </el-radio-group>
      </el-form-item>
      <el-form-item v-if="form.auth_type === 'password'" label="密码">
        <el-input v-model="form.password" type="password" show-password :placeholder="editRow ? '留空则不修改' : ''" />
      </el-form-item>
      <el-form-item v-else label="私钥">
        <el-input v-model="form.private_key" type="textarea" :rows="6" :placeholder="editRow ? '留空则不修改' : '-----BEGIN ... KEY-----'" />
      </el-form-item>
      <el-form-item label="提权 (sudo)">
        <el-switch v-model="form.become" />
      </el-form-item>
      <el-form-item v-if="form.become" label="提权密码">
        <el-input v-model="form.become_password" type="password" show-password :placeholder="editRow ? '留空则不修改' : ''" />
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
import {
  getCredentials, createCredential, updateCredential, deleteCredential,
} from '../../api/tasks'

const loading = ref(false)
const submitting = ref(false)
const tableData = ref([])

const dialogVisible = ref(false)
const editRow = ref(null)
const formRef = ref()
const emptyForm = () => ({
  name: '', username: 'root', auth_type: 'password',
  password: '', private_key: '', become: false, become_password: '',
})
const form = reactive(emptyForm())
const rules = {
  name: [{ required: true, message: '请输入名称' }],
  username: [{ required: true, message: '请输入登录用户' }],
}

const formatTime = (t) => (t ? t.slice(0, 19).replace('T', ' ') : '')

async function fetchData() {
  loading.value = true
  try {
    const res = await getCredentials({ page_size: 200 })
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
      name: row.name, username: row.username, auth_type: row.auth_type,
      become: row.become, password: '', private_key: '', become_password: '',
    })
  }
  dialogVisible.value = true
  formRef.value?.clearValidate()
}

async function handleSubmit() {
  await formRef.value.validate()
  submitting.value = true
  try {
    if (editRow.value) {
      await updateCredential(editRow.value.id, { ...form })
      ElMessage.success('更新成功')
    } else {
      await createCredential({ ...form })
      ElMessage.success('新增成功')
    }
    dialogVisible.value = false
    fetchData()
  } finally {
    submitting.value = false
  }
}

async function handleDelete(row) {
  await ElMessageBox.confirm(`确定删除凭据「${row.name}」吗？`, '警告', { type: 'warning' })
  await deleteCredential(row.id)
  ElMessage.success('删除成功')
  fetchData()
}

onMounted(fetchData)
</script>

<style scoped>
.toolbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.hint { color: #909399; font-size: 13px; }
</style>
