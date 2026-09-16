<template>
  <el-card shadow="never">
    <template #header>
      <div class="card-header">
        <span>飞书绑定</span>
        <span class="tip">
          决定哪个飞书账号能以哪个平台账号登录与发起执行。用飞书登录一次会按企业邮箱
          自动绑定；匹配不上时在这里手工绑。
        </span>
      </div>
    </template>

    <div class="toolbar">
      <el-input
        v-model="keyword"
        placeholder="搜索平台用户 / 飞书昵称 / 邮箱"
        clearable
        :prefix-icon="Search"
        style="width:260px"
        @change="load"
      />
      <el-button type="primary" :icon="Plus" @click="openDialog">手工绑定</el-button>
    </div>

    <el-table v-loading="loading" :data="rows" stripe>
      <el-table-column label="平台用户" min-width="150">
        <template #default="{ row }">
          {{ row.username }}
          <el-tag v-if="!row.user_is_active" type="info" size="small">已停用</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="user_email" label="平台邮箱" min-width="180">
        <template #default="{ row }">
          <span v-if="row.user_email">{{ row.user_email }}</span>
          <span v-else class="unset">—</span>
        </template>
      </el-table-column>
      <el-table-column prop="feishu_name" label="飞书昵称" min-width="120">
        <template #default="{ row }">
          <span v-if="row.feishu_name">{{ row.feishu_name }}</span>
          <span v-else class="unset">—</span>
        </template>
      </el-table-column>
      <el-table-column prop="feishu_email" label="飞书邮箱" min-width="180">
        <template #default="{ row }">
          <span v-if="row.feishu_email">{{ row.feishu_email }}</span>
          <span v-else class="unset">—</span>
        </template>
      </el-table-column>
      <el-table-column label="绑定来源" width="120">
        <template #default="{ row }">
          <el-tag :type="row.source === 'manual' ? 'success' : 'warning'" size="small">
            {{ row.source_display }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="最近飞书登录" width="170">
        <template #default="{ row }">{{ formatTime(row.last_login_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="100" fixed="right">
        <template #default="{ row }">
          <el-button size="small" type="danger" :icon="Delete" @click="unbind(row)">解绑</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-pagination
      v-model:current-page="page"
      v-model:page-size="pageSize"
      :page-sizes="[20, 50, 100]"
      :total="total"
      layout="total, sizes, prev, pager, next"
      style="margin-top:12px;justify-content:flex-end"
      @size-change="load"
      @current-change="load"
    />
  </el-card>

  <el-dialog v-model="dialogVisible" title="手工绑定飞书账号" width="520px">
    <el-alert type="info" :closable="false" show-icon style="margin-bottom:14px">
      先按邮箱或手机号在飞书通讯录里查到本人，再选择要绑定的平台账号。
    </el-alert>
    <el-form :model="form" label-width="110px">
      <el-form-item label="飞书邮箱/手机">
        <div class="url-row">
          <el-input
            v-model="form.query"
            placeholder="企业邮箱或手机号"
            @keyup.enter="doLookup"
          />
          <el-button :loading="looking" @click="doLookup">查询</el-button>
        </div>
      </el-form-item>
      <el-form-item v-if="found.union_id" label="查到的成员">
        <div>
          <b>{{ found.name || '（未取到昵称）' }}</b>
          <div class="tip">UnionID：{{ found.union_id }}</div>
        </div>
      </el-form-item>
      <el-form-item label="平台账号">
        <el-select
          v-model="form.user"
          filterable
          remote
          :remote-method="searchUsers"
          :loading="userLoading"
          placeholder="搜索用户名"
          style="width:100%"
        >
          <el-option
            v-for="u in userOptions"
            :key="u.id"
            :label="u.email ? `${u.username}（${u.email}）` : u.username"
            :value="u.id"
          />
        </el-select>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="dialogVisible = false">取消</el-button>
      <el-button
        type="primary"
        :loading="submitting"
        :disabled="!found.union_id || !form.user"
        @click="save"
      >
        绑定
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup>
/**
 * 飞书绑定管理（系统管理 → 飞书绑定，需 account write 档）。
 *
 * 只有「查 / 新增 / 解绑」三件事：改绑等于解绑再绑，两步各自留痕比一次静默改值
 * 清楚，后端也没开更新接口。
 */
import { ref, reactive, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Delete, Plus, Search } from '@element-plus/icons-vue'
import {
  createBinding, deleteBinding, getBindings, lookupFeishuUser,
} from '../../api/feishu'
import { getUsers } from '../../api/users'

const loading = ref(false)
const rows = ref([])
const keyword = ref('')
const page = ref(1)
const pageSize = ref(20)
const total = ref(0)

const dialogVisible = ref(false)
const submitting = ref(false)
const looking = ref(false)
const userLoading = ref(false)
const userOptions = ref([])
const found = reactive({ union_id: '', open_id: '', name: '', email: '', mobile: '' })
const form = reactive({ query: '', user: null })

const formatTime = (t) => (t ? t.slice(0, 19).replace('T', ' ') : '从未登录')

async function load() {
  loading.value = true
  try {
    const res = await getBindings({
      keyword: keyword.value || undefined,
      page: page.value,
      page_size: pageSize.value,
    })
    rows.value = res.data.results
    total.value = res.data.total
  } finally {
    loading.value = false
  }
}

function openDialog() {
  Object.assign(found, { union_id: '', open_id: '', name: '', email: '', mobile: '' })
  Object.assign(form, { query: '', user: null })
  userOptions.value = []
  dialogVisible.value = true
  searchUsers('')
}

async function doLookup() {
  const query = form.query.trim()
  if (!query) {
    ElMessage.warning('请输入飞书邮箱或手机号')
    return
  }
  looking.value = true
  try {
    // 带 @ 当邮箱，否则按手机号：让管理员在一个输入框里搞定，不用先选类型
    const payload = query.includes('@') ? { email: query } : { mobile: query }
    const res = await lookupFeishuUser(payload)
    Object.assign(found, res.data)
  } finally {
    looking.value = false
  }
}

async function searchUsers(query) {
  userLoading.value = true
  try {
    const res = await getUsers({ keyword: query || undefined, page_size: 50 })
    userOptions.value = res.data.results || res.data
  } finally {
    userLoading.value = false
  }
}

async function save() {
  submitting.value = true
  try {
    await createBinding({
      user: form.user,
      union_id: found.union_id,
      open_id: found.open_id,
      feishu_name: found.name,
      feishu_email: found.email,
    })
    ElMessage.success('已绑定')
    dialogVisible.value = false
    load()
  } finally {
    submitting.value = false
  }
}

async function unbind(row) {
  await ElMessageBox.confirm(
    `解绑后「${row.username}」将无法用飞书登录，也无法从飞书发起执行或处理暂停。`
    + '若该账号没有可用密码（自动创建的），解绑后将再也无法登录。确认解绑吗？',
    '解除飞书绑定', { type: 'warning', confirmButtonText: '确认解绑' },
  )
  await deleteBinding(row.id)
  ElMessage.success('已解绑')
  load()
}

onMounted(load)
</script>

<style scoped>
.card-header {
  display: flex;
  align-items: center;
  gap: 12px;
}
.toolbar {
  display: flex;
  justify-content: space-between;
  margin-bottom: 12px;
}
.url-row {
  display: flex;
  gap: 8px;
  width: 100%;
}
.tip {
  font-size: 12px;
  color: #909399;
  line-height: 1.6;
}
.unset {
  color: #C0C4CC;
}
</style>
