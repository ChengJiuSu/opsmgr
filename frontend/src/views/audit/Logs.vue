<template>
  <el-card shadow="never">
    <div class="toolbar">
      <el-input v-model="filter.username" placeholder="用户名" clearable style="width:130px" @keyup.enter="handleSearch" @clear="handleSearch" />
      <el-input v-model="filter.keyword" placeholder="路径关键字" clearable style="width:160px" @keyup.enter="handleSearch" @clear="handleSearch" />
      <el-select v-model="filter.method" placeholder="HTTP 方法" clearable style="width:120px" @change="handleSearch">
        <el-option v-for="m in ['GET','POST','PUT','PATCH','DELETE']" :key="m" :label="m" :value="m" />
      </el-select>
      <el-input v-model="filter.status_code" placeholder="状态码" clearable style="width:100px" @keyup.enter="handleSearch" @clear="handleSearch" />
      <el-date-picker
        v-model="dateRange"
        type="daterange"
        value-format="YYYY-MM-DD"
        start-placeholder="开始日期"
        end-placeholder="结束日期"
        style="width:240px"
        @change="handleSearch"
      />
      <el-button :icon="Search" @click="handleSearch">查询</el-button>
    </div>

    <el-table v-loading="loading" :data="tableData" stripe>
      <el-table-column prop="username" label="用户" width="100" />
      <el-table-column label="方法" width="90">
        <template #default="{ row }">
          <el-tag size="small" :type="methodType(row.method)">{{ row.method }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="path" label="路径" show-overflow-tooltip />
      <el-table-column prop="ip" label="IP" width="130" />
      <el-table-column prop="status_code" label="状态码" width="90">
        <template #default="{ row }">
          <el-tag size="small" :type="row.status_code < 400 ? 'success' : 'danger'">
            {{ row.status_code }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="created_at" label="时间" width="165">
        <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
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
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { Search } from '@element-plus/icons-vue'
import { getAuditLogs } from '../../api/audit'

const loading = ref(false)
const tableData = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const filter = reactive({ username: '', keyword: '', method: '', status_code: '' })
const dateRange = ref([])

const methodType = (m) => ({ GET: 'info', POST: 'success', PUT: 'warning', PATCH: 'warning', DELETE: 'danger' }[m] || 'info')
const formatTime = (t) => t ? t.replace('T', ' ').slice(0, 19) : ''

async function fetchData() {
  loading.value = true
  try {
    const params = { page: page.value, page_size: pageSize.value }
    if (filter.username) params.username = filter.username
    if (filter.keyword) params.keyword = filter.keyword
    if (filter.method) params.method = filter.method
    if (filter.status_code) params.status_code = filter.status_code
    if (dateRange.value?.length === 2) {
      params.date_from = dateRange.value[0]
      params.date_to = dateRange.value[1]
    }
    const res = await getAuditLogs(params)
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

onMounted(fetchData)
</script>

<style scoped>
.toolbar { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }
</style>
