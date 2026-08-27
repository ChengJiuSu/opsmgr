<template>
  <el-card shadow="never">
    <div class="toolbar">
      <span class="hint">
        角色 = 模块权限矩阵 + 能力位。用户未绑角色时按「是否管理员」回退（存量账号行为不变）。
        权限档：无权限 / 只读 / 可读可新增 / 读写；「可读可新增」对应资产模块「可查可增不可改删」。
      </span>
      <div>
        <el-button :icon="Refresh" @click="fetchData">刷新</el-button>
        <el-button type="primary" :icon="Plus" @click="openDialog()">新增角色</el-button>
      </div>
    </div>

    <el-table v-loading="loading" :data="tableData" stripe>
      <el-table-column prop="name" label="角色" min-width="120">
        <template #default="{ row }">
          {{ row.name }}
          <el-tag v-if="row.is_builtin" size="small" type="info" class="tag-gap">内置</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="code" label="标识" width="110">
        <template #default="{ row }"><code>{{ row.code }}</code></template>
      </el-table-column>
      <el-table-column
        v-for="module in meta.modules" :key="module.value"
        :label="shortLabel(module.label)" width="110"
      >
        <template #default="{ row }">
          <el-tag size="small" :type="levelType(row.permissions?.[module.value])">
            {{ levelLabel(row.permissions?.[module.value]) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="执行能力" width="150">
        <template #default="{ row }">
          <el-tag v-if="row.can_execute" size="small" type="success">可执行</el-tag>
          <el-tag v-else size="small" type="info">不可执行</el-tag>
          <el-tag
            v-if="row.can_exec_prod" size="small" type="danger" class="tag-gap"
          >含生产</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="user_count" label="用户数" width="80" />
      <el-table-column label="状态" width="80">
        <template #default="{ row }">
          <el-tag size="small" :type="row.enabled ? 'success' : 'info'">
            {{ row.enabled ? '启用' : '停用' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="150" fixed="right">
        <template #default="{ row }">
          <el-button size="small" :icon="Edit" @click="openDialog(row)">编辑</el-button>
          <el-button
            v-if="!row.is_builtin" size="small" type="danger" :icon="Delete"
            @click="handleDelete(row)"
          />
        </template>
      </el-table-column>
    </el-table>
  </el-card>

  <el-dialog v-model="dialogVisible" :title="editRow ? '编辑角色' : '新增角色'" width="680px">
    <el-form ref="formRef" :model="form" :rules="rules" label-width="90px">
      <el-row :gutter="16">
        <el-col :span="12">
          <el-form-item label="角色名称" prop="name">
            <el-input v-model="form.name" placeholder="如 发布员" />
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item label="标识" prop="code">
            <el-input v-model="form.code" :disabled="!!editRow" placeholder="小写字母/数字/下划线" />
          </el-form-item>
        </el-col>
      </el-row>
      <el-form-item label="说明">
        <el-input v-model="form.remark" placeholder="这个角色给谁用、能干什么" />
      </el-form-item>

      <el-divider content-position="left">模块权限</el-divider>
      <el-form-item
        v-for="module in meta.modules" :key="module.value" :label="shortLabel(module.label)"
      >
        <el-radio-group v-model="form.permissions[module.value]">
          <el-radio-button v-for="lv in meta.levels" :key="lv.value" :value="lv.value">
            {{ lv.label }}
          </el-radio-button>
        </el-radio-group>
        <div class="tip">{{ module.label }}</div>
      </el-form-item>

      <el-divider content-position="left">执行能力</el-divider>
      <el-form-item label="可执行">
        <el-switch v-model="form.can_execute" />
        <span class="tip">可发起任务流执行、可处理暂停中的任务</span>
      </el-form-item>
      <el-form-item label="含生产">
        <el-switch v-model="form.can_exec_prod" :disabled="!form.can_execute" />
        <span class="tip danger" v-if="form.can_exec_prod">
          可对生产环境发起执行 —— 错了就是已经推上去了，只给需要的人
        </span>
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
import { Plus, Edit, Delete, Refresh } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { getRoles, getRoleMeta, createRole, updateRole, deleteRole } from '../../api/users'

const loading = ref(false)
const submitting = ref(false)
const tableData = ref([])
// 模块清单与权限档由后端给（roles.MODULES / LEVELS），前端不硬编码——
// 加一个模块时不用同步改两边
const meta = reactive({ modules: [], levels: [], capabilities: [] })

const dialogVisible = ref(false)
const editRow = ref(null)
const formRef = ref()
const form = reactive({
  name: '', code: '', remark: '', permissions: {},
  can_execute: false, can_exec_prod: false, enabled: true,
})
const rules = {
  name: [{ required: true, message: '请输入角色名称' }],
  code: [{ required: true, message: '请输入角色标识' }],
}

// 模块说明形如「资产与配置（服务器、字典…）」，表头只取括号前的短名
const shortLabel = (label) => String(label).split('（')[0]
const levelLabel = (level) =>
  meta.levels.find((item) => item.value === (level || 'none'))?.label || '无权限'
const levelType = (level) => ({
  write: 'success', read_create: 'warning', read: 'info', none: 'danger',
}[level || 'none'])

async function fetchData() {
  loading.value = true
  try {
    const res = await getRoles()
    tableData.value = Array.isArray(res) ? res : res.data || []
  } finally {
    loading.value = false
  }
}

async function loadMeta() {
  const res = await getRoleMeta()
  Object.assign(meta, res.data || {})
}

function openDialog(row = null) {
  editRow.value = row
  const permissions = {}
  meta.modules.forEach((module) => {
    permissions[module.value] = row?.permissions?.[module.value] || 'none'
  })
  Object.assign(form, {
    name: row?.name || '', code: row?.code || '', remark: row?.remark || '',
    permissions,
    can_execute: row ? row.can_execute : false,
    can_exec_prod: row ? row.can_exec_prod : false,
    enabled: row ? row.enabled : true,
  })
  dialogVisible.value = true
  formRef.value?.clearValidate()
}

async function handleSubmit() {
  await formRef.value.validate()
  submitting.value = true
  try {
    // 关掉「可执行」时连带清掉「含生产」，避免留下一个不生效但看着开着的开关
    const payload = { ...form, can_exec_prod: form.can_execute && form.can_exec_prod }
    if (editRow.value) {
      await updateRole(editRow.value.id, payload)
      ElMessage.success('已保存（绑定该角色的用户立即生效）')
    } else {
      await createRole(payload)
      ElMessage.success('已新增')
    }
    dialogVisible.value = false
    fetchData()
  } finally {
    submitting.value = false
  }
}

async function handleDelete(row) {
  await ElMessageBox.confirm(
    `确定删除角色「${row.name}」吗？仍有用户在用时会被拒绝，请先改这些用户的角色。`,
    '警告', { type: 'warning', confirmButtonText: '删除', confirmButtonClass: 'el-button--danger' },
  )
  await deleteRole(row.id)
  ElMessage.success('已删除')
  fetchData()
}

onMounted(async () => {
  await loadMeta()
  fetchData()
})
</script>

<style scoped>
.toolbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; gap: 12px; }
.hint { color: #909399; font-size: 13px; line-height: 1.6; }
.tag-gap { margin-left: 6px; }
.tip { color: #909399; font-size: 12px; margin-left: 8px; }
.tip.danger { color: var(--el-color-danger); }
</style>
