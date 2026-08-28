<template>
  <el-card shadow="never">
    <!-- 工具栏 -->
    <div class="toolbar">
      <div class="toolbar-left">
        <el-input
          v-model="filter.keyword"
          :placeholder="`搜索${cfg.label}名称/编码`"
          clearable
          style="width:220px"
          @input="searchDebounced"
          @keyup.enter="handleSearch"
          @clear="handleSearch"
        />
        <el-select v-model="filter.enabled" placeholder="启用状态" clearable style="width:120px" @change="handleSearch">
          <el-option label="启用" value="true" />
          <el-option label="停用" value="false" />
        </el-select>
      </div>
      <el-button type="primary" :icon="Plus" @click="openDialog()">新增{{ cfg.label }}</el-button>
    </div>

    <!-- 表格 -->
    <el-table v-loading="loading" :data="tableData" stripe>
      <el-table-column prop="name" label="名称" min-width="120" />
      <el-table-column prop="code" label="编码" min-width="110">
        <template #default="{ row }">
          <el-tag size="small" type="info">{{ row.code }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column
        v-for="col in cfg.columns"
        :key="col.prop"
        :label="col.label"
        min-width="100"
      >
        <template #default="{ row }">
          <el-tag v-if="col.bool" size="small" :type="row[col.prop] ? 'danger' : 'info'">
            {{ row[col.prop] ? '是' : '否' }}
          </el-tag>
          <span v-else-if="col.map">{{ col.map[row[col.prop]] || row[col.prop] }}</span>
          <span v-else>{{ row[col.prop] || '-' }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="sort_order" label="排序" width="70" />
      <el-table-column label="启用" width="80">
        <template #default="{ row }">
          <!-- 启停属于修改操作，普通用户仅展示状态 -->
          <el-switch v-if="canWrite" :model-value="row.enabled" @change="(v) => toggleEnabled(row, v)" />
          <el-tag v-else size="small" :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '启用' : '停用' }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="remark" label="备注" min-width="120" show-overflow-tooltip />
      <el-table-column v-if="canWrite" label="操作" width="140" fixed="right">
        <template #default="{ row }">
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
  <el-dialog v-model="dialogVisible" :title="(editRow ? '编辑' : '新增') + cfg.label" width="480px">
    <el-form ref="formRef" :model="form" :rules="rules" label-width="90px">
      <el-form-item label="名称" prop="name">
        <el-input v-model="form.name" />
      </el-form-item>
      <el-form-item label="编码" prop="code">
        <el-input v-model="form.code" placeholder="唯一编码，如 prod / aliyun" :disabled="!!editRow" />
      </el-form-item>
      <!-- 各字典差异字段 -->
      <el-form-item v-for="field in cfg.fields" :key="field.prop" :label="field.label">
        <el-select v-if="field.type === 'select'" v-model="form[field.prop]" style="width:100%">
          <el-option v-for="opt in field.options" :key="opt.value" :label="opt.label" :value="opt.value" />
        </el-select>
        <el-switch v-else-if="field.type === 'switch'" v-model="form[field.prop]" />
        <el-input v-else v-model="form[field.prop]" />
      </el-form-item>
      <el-form-item label="排序">
        <el-input-number v-model="form.sort_order" :min="0" style="width:100%" />
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
import { ref, reactive, computed, watch, onMounted, onUnmounted } from 'vue'
import { debounce } from '../../utils/debounce'
import { Plus, Edit, Delete } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { projectGroupApi, channelApi, environmentApi, cloudProviderApi, serviceTypeApi } from '../../api/assets'
import { useAuthStore } from '../../stores/auth'

const authStore = useAuthStore()
// 按钮可见性跟角色矩阵走（字典是数据范围与生产标记的来源，单独一个模块）
const canWrite = computed(() => authStore.can('dict', 'write'))

// 路由以 props 方式传入字典类型（见 router/index.js）
const props = defineProps({
  type: { type: String, required: true },
})

// 各字典模块差异配置：接口、差异列、差异表单项、表单默认值
const dictConfigs = {
  'project-groups': {
    api: projectGroupApi,
    label: '项目组',
    columns: [
      { prop: 'owner', label: '负责人' },
      { prop: 'contact', label: '联系方式' },
    ],
    fields: [
      { prop: 'owner', label: '负责人', type: 'input' },
      { prop: 'contact', label: '联系方式', type: 'input' },
    ],
    defaults: { owner: '', contact: '' },
  },
  channels: {
    api: channelApi,
    label: '渠道',
    columns: [
      { prop: 'platform', label: '平台', map: { all: '全平台', android: '安卓', ios: 'iOS' } },
    ],
    fields: [
      {
        prop: 'platform', label: '平台', type: 'select',
        options: [
          { label: '全平台', value: 'all' },
          { label: '安卓', value: 'android' },
          { label: 'iOS', value: 'ios' },
        ],
      },
    ],
    defaults: { platform: 'all' },
  },
  environments: {
    api: environmentApi,
    label: '环境',
    columns: [{ prop: 'is_prod', label: '生产环境', bool: true }],
    fields: [{ prop: 'is_prod', label: '生产环境', type: 'switch' }],
    defaults: { is_prod: false },
  },
  'cloud-providers': {
    api: cloudProviderApi,
    label: '云厂商',
    columns: [],
    fields: [],
    defaults: {},
  },
  'service-types': {
    api: serviceTypeApi,
    label: '服务类型',
    columns: [],
    fields: [],
    defaults: {},
  },
}

const cfg = computed(() => dictConfigs[props.type])

const loading = ref(false)
const submitting = ref(false)
const tableData = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const filter = reactive({ keyword: '', enabled: '' })
const dialogVisible = ref(false)
const editRow = ref(null)
const formRef = ref()

const emptyForm = () => ({
  name: '', code: '', enabled: true, sort_order: 0, remark: '', ...cfg.value.defaults,
})
const form = reactive(emptyForm())
const rules = {
  name: [{ required: true, message: '请输入名称' }],
  code: [
    { required: true, message: '请输入编码' },
    { pattern: /^[-a-zA-Z0-9_]+$/, message: '仅限字母、数字、中划线、下划线' },
  ],
}

// 搜索：回到第一页再查，避免停留在越界页码上
function handleSearch() {
  searchDebounced.cancel()
  page.value = 1
  fetchData()
}

// 边输边查（300ms 防抖），无需回车
const searchDebounced = debounce(handleSearch)
onUnmounted(() => searchDebounced.cancel())

async function fetchData() {
  loading.value = true
  try {
    const params = { page: page.value, page_size: pageSize.value }
    if (filter.keyword) params.keyword = filter.keyword
    if (filter.enabled) params.enabled = filter.enabled
    const res = await cfg.value.api.list(params)
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
      await cfg.value.api.update(editRow.value.id, form)
      ElMessage.success('更新成功')
    } else {
      await cfg.value.api.create(form)
      ElMessage.success('新增成功')
    }
    dialogVisible.value = false
    fetchData()
  } finally {
    submitting.value = false
  }
}

// 表格内快捷启停（失败时刷新还原）
async function toggleEnabled(row, value) {
  try {
    await cfg.value.api.update(row.id, { ...row, enabled: value })
    row.enabled = value
  } catch {
    fetchData()
  }
}

async function handleDelete(row) {
  await ElMessageBox.confirm(
    `确定删除${cfg.value.label}「${row.name}」吗？仍被服务器引用时将无法删除。`,
    '警告',
    { type: 'warning' },
  )
  await cfg.value.api.remove(row.id)
  ElMessage.success('删除成功')
  fetchData()
}

// 同一组件在四个字典路由间复用，切换路由时重置并重新加载
watch(() => props.type, () => {
  page.value = 1
  filter.keyword = ''
  filter.enabled = ''
  fetchData()
})

onMounted(fetchData)
</script>

<style scoped>
.toolbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.toolbar-left { display: flex; gap: 8px; }
</style>
