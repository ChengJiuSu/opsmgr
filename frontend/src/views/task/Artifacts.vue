<template>
  <el-card shadow="never">
    <div class="toolbar">
      <div class="toolbar-left">
        <el-input v-model="filter.keyword" placeholder="版本 / 文件名 / Job" clearable style="width:180px" @input="searchDebounced" @keyup.enter="handleSearch" @clear="handleSearch" />
        <el-select v-model="filter.project_group" placeholder="项目组" clearable filterable style="width:130px" @change="handleSearch">
          <el-option v-for="d in dictOptions.projects" :key="d.id" :label="d.name" :value="d.id" />
        </el-select>
        <el-select v-model="filter.channel" placeholder="渠道" clearable filterable style="width:120px" @change="handleSearch">
          <el-option v-for="d in dictOptions.channels" :key="d.id" :label="d.name" :value="d.id" />
        </el-select>
        <el-select v-model="filter.env" placeholder="环境" clearable filterable style="width:120px" @change="handleSearch">
          <el-option v-for="d in dictOptions.envs" :key="d.id" :label="d.name" :value="d.id" />
        </el-select>
        <el-select v-model="filter.service_type" placeholder="服务类型" clearable filterable style="width:130px" @change="handleSearch">
          <el-option v-for="d in dictOptions.serviceTypes" :key="d.id" :label="d.name" :value="d.id" />
        </el-select>
        <el-button :icon="Search" @click="handleSearch">查询</el-button>
        <el-button :icon="Refresh" @click="fetchData">刷新</el-button>
      </div>
    </div>

    <el-alert type="info" :closable="false" style="margin-bottom: 12px">
      构建制品由 Jenkins CI 完成后<b>脚本上报</b>（<code>POST /api/artifacts/report/</code>，X-API-KEY 鉴权），
      按<b>项目组/渠道/环境/服务类型/版本</b>唯一登记。CD 任务执行时按<b>模块的服务类型</b>取该维度
      <b>最新制品</b>并注入 <code>package_url / package_version / package_md5</code>，模块参数
      <code>artifact_version</code> 可锁定版本。服务类型严格匹配、不回退，所以上报时必须带
      <code>service_type</code>，否则按服务类型部署的模块取不到包。
    </el-alert>

    <el-table v-loading="loading" :data="tableData" stripe>
      <el-table-column label="项目组 / 渠道 / 环境" min-width="190" show-overflow-tooltip>
        <template #default="{ row }">
          {{ row.project_group_name }} / {{ row.channel_name }} / {{ row.environment_name }}
        </template>
      </el-table-column>
      <el-table-column label="服务类型" min-width="120" show-overflow-tooltip>
        <template #default="{ row }">
          <span v-if="row.service_type_name">{{ row.service_type_name }}</span>
          <!-- 存量制品（字段上线前上报）：按服务类型取包的模块不会命中它 -->
          <el-tooltip v-else content="上报时未带服务类型，按服务类型取包的模块不会取到它" placement="top">
            <span class="muted">未指定</span>
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column prop="version" label="版本" min-width="180" show-overflow-tooltip>
        <template #default="{ row }"><code class="mono">{{ row.version }}</code></template>
      </el-table-column>
      <el-table-column prop="original_name" label="文件名" min-width="220" show-overflow-tooltip />
      <el-table-column label="MD5" width="300">
        <template #default="{ row }">
          <el-tooltip content="点击复制 MD5" placement="top">
            <code class="mono md5" @click="copyMd5(row)">{{ row.md5 }}</code>
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column label="大小" width="100" align="right">
        <template #default="{ row }">{{ humanSize(row.size) }}</template>
      </el-table-column>
      <el-table-column label="Jenkins" min-width="200" show-overflow-tooltip>
        <template #default="{ row }">
          <span v-if="row.jenkins_job">{{ row.jenkins_job }}<template v-if="row.build_number"> #{{ row.build_number }}</template></span>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column prop="reported_at" label="上报时间" width="170">
        <template #default="{ row }">{{ formatTime(row.reported_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="150" fixed="right" align="center">
        <template #default="{ row }">
          <el-button size="small" :icon="Download" @click="handleDownload(row)">下载</el-button>
          <el-button v-if="canWrite" size="small" type="danger" :icon="Delete" @click="handleDelete(row)" />
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
      @current-change="fetchData"
      @size-change="handleSearch"
    />
  </el-card>
</template>

<script setup>
import { ref, computed, reactive, onMounted, onUnmounted } from 'vue'
import { debounce } from '../../utils/debounce'
import { Delete, Download, Refresh, Search } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useAuthStore } from '../../stores/auth'
import { fetchDictOptions, projectGroupApi, channelApi, environmentApi, serviceTypeApi } from '../../api/assets'
import { deleteArtifact, getArtifacts } from '../../api/artifacts'

const authStore = useAuthStore()
// 按钮可见性跟角色矩阵走（制品只能由 CI 上报，页面上只有删）
const canWrite = computed(() => authStore.can('artifact', 'write'))
const loading = ref(false)
const tableData = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const filter = reactive({ keyword: '', project_group: '', channel: '', env: '', service_type: '' })
const dictOptions = reactive({ projects: [], channels: [], envs: [], serviceTypes: [] })

const formatTime = (t) => (t ? t.slice(0, 19).replace('T', ' ') : '')

function humanSize(bytes) {
  if (!bytes && bytes !== 0) return '—'
  const units = ['B', 'KB', 'MB', 'GB']
  let size = bytes
  let i = 0
  while (size >= 1024 && i < units.length - 1) {
    size /= 1024
    i += 1
  }
  return `${size.toFixed(i ? 1 : 0)} ${units[i]}`
}

async function loadDictOptions() {
  if (dictOptions.projects.length) return
  const [proj, chan, env, svc] = await Promise.allSettled([
    fetchDictOptions(projectGroupApi), fetchDictOptions(channelApi), fetchDictOptions(environmentApi),
    fetchDictOptions(serviceTypeApi),
  ])
  if (proj.status === 'fulfilled') dictOptions.projects = proj.value.data?.results || []
  if (chan.status === 'fulfilled') dictOptions.channels = chan.value.data?.results || []
  if (env.status === 'fulfilled') dictOptions.envs = env.value.data?.results || []
  if (svc.status === 'fulfilled') dictOptions.serviceTypes = svc.value.data?.results || []
}

async function fetchData() {
  loading.value = true
  try {
    const params = { page: page.value, page_size: pageSize.value }
    for (const k of ['keyword', 'project_group', 'channel', 'env', 'service_type']) {
      if (filter[k]) params[k] = filter[k]
    }
    const res = await getArtifacts(params)
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

function handleDownload(row) {
  // download_url 自带 HMAC 签名参数，免登录直接下载
  window.open(row.download_url, '_blank')
}

async function copyMd5(row) {
  try {
    await navigator.clipboard.writeText(row.md5)
    ElMessage.success('MD5 已复制')
  } catch {
    ElMessage.info(row.md5)
  }
}

async function handleDelete(row) {
  await ElMessageBox.confirm(
    `确定删除制品「${row.version}（${row.original_name}）」吗？磁盘文件将一并删除。`,
    '警告', { type: 'warning' },
  )
  await deleteArtifact(row.id)
  ElMessage.success('删除成功')
  fetchData()
}

onMounted(() => {
  loadDictOptions()
  fetchData()
})
</script>

<style scoped>
.toolbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.toolbar-left { display: flex; gap: 8px; flex-wrap: wrap; }
.muted { color: #c0c4cc; }
code { background: #f5f7fa; padding: 1px 6px; border-radius: 3px; font-size: 12px; }
.mono { font-family: Menlo, Consolas, "Courier New", monospace; letter-spacing: 0; white-space: nowrap; }
.md5 { cursor: pointer; color: #606266; }
.md5:hover { color: var(--el-color-primary); }
</style>
