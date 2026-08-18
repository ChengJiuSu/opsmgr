<template>
  <el-card shadow="never">
    <div class="toolbar">
      <div class="toolbar-left">
        <el-breadcrumb separator="/">
          <el-breadcrumb-item>
            <a class="crumb" @click="enter('')">日志根目录</a>
          </el-breadcrumb-item>
          <el-breadcrumb-item v-for="(crumb, i) in crumbs" :key="crumb.path">
            <a v-if="i < crumbs.length - 1" class="crumb" @click="enter(crumb.path)">{{ crumb.name }}</a>
            <span v-else>{{ crumb.name }}</span>
          </el-breadcrumb-item>
        </el-breadcrumb>
      </div>
      <div class="toolbar-left">
        <el-button :icon="Back" :disabled="parent === null" @click="enter(parent)">上级目录</el-button>
        <el-button :icon="Refresh" @click="fetchData">刷新</el-button>
      </div>
    </div>

    <el-alert type="info" :closable="false" style="margin-bottom: 12px">
      任务执行的落盘日志与执行现场归档（主机文件 <code>inventory.ini</code>、变量文件
      <code>vars.yml</code>、执行命令 <code>command.txt</code>）按
      <code>&lt;任务 id&gt;/&lt;执行记录 id&gt;.log</code> 与
      <code>&lt;执行记录 id&gt;.d/step&lt;序号&gt;/&lt;通道&gt;/</code> 存放。
      文件含变量值，仅管理员可见；过保留期由清理任务删除。
      <span class="muted">根目录：{{ root || '—' }}</span>
    </el-alert>

    <el-table v-loading="loading" :data="entries" stripe @row-dblclick="onRowDblclick">
      <el-table-column label="名称" min-width="240" show-overflow-tooltip>
        <template #default="{ row }">
          <el-icon class="row-icon"><Folder v-if="row.type === 'dir'" /><Document v-else /></el-icon>
          <a v-if="row.type === 'dir'" class="crumb" @click="enter(row.path)">{{ row.name }}</a>
          <a v-else class="crumb" @click="openFile(row)">{{ row.name }}</a>
        </template>
      </el-table-column>
      <el-table-column label="说明" min-width="220" show-overflow-tooltip>
        <template #default="{ row }">
          <span :class="{ unset: !row.label }">{{ row.label || '—' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="大小" width="110" align="right">
        <template #default="{ row }">
          <span :class="{ unset: row.size === null }">{{ row.size === null ? '—' : formatSize(row.size) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="修改时间" width="180">
        <template #default="{ row }">{{ formatTime(row.modified_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="150" fixed="right">
        <template #default="{ row }">
          <template v-if="row.type === 'file'">
            <el-button link type="primary" size="small" @click="openFile(row)">查看</el-button>
            <el-button link type="primary" size="small" @click="download(row)">下载</el-button>
          </template>
          <el-button v-else link type="primary" size="small" @click="enter(row.path)">进入</el-button>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty :image-size="80" description="该目录为空（或文件已过保留期被清理）" />
      </template>
    </el-table>

    <!-- 文件查看：日志含 ANSI 彩色转义，按 utils/ansi 渲染；大文件分段读取 -->
    <el-dialog v-model="viewerVisible" :title="viewer.path" width="960px" top="6vh">
      <div class="viewer-bar">
        <span class="muted">{{ formatSize(viewer.size) }}</span>
        <el-radio-group v-model="viewer.mode" size="small" @change="reload">
          <el-radio-button value="head">从头读</el-radio-button>
          <el-radio-button value="tail">读末尾</el-radio-button>
        </el-radio-group>
        <el-button size="small" :icon="Download" @click="download({ path: viewer.path, name: viewer.name })">
          下载原文件
        </el-button>
        <span v-if="!viewer.eof" class="muted">已读到 {{ formatSize(viewer.read) }}，未到末尾</span>
      </div>
      <pre v-loading="viewer.loading" class="viewer-body" v-html="viewerHtml" />
      <template #footer>
        <el-button v-if="!viewer.eof" type="primary" :loading="viewer.loading" @click="loadMore">
          继续加载
        </el-button>
        <el-button @click="viewerVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </el-card>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { Back, Document, Download, Folder, Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { getLogFiles, getLogFileContent, downloadLogFile } from '../../api/tasks'
import { ansiToHtml } from '../../utils/ansi'

const loading = ref(false)
const entries = ref([])
const path = ref('')
const parent = ref(null)
const root = ref('')

// 面包屑：根目录单独渲染，这里只列 path 的各层
const crumbs = computed(() => {
  const parts = path.value.split('/').filter(Boolean)
  return parts.map((name, i) => ({ name, path: parts.slice(0, i + 1).join('/') }))
})

async function fetchData() {
  loading.value = true
  try {
    const res = await getLogFiles(path.value)
    entries.value = res.data?.entries || []
    parent.value = res.data?.parent ?? null
    root.value = res.data?.root || ''
  } finally {
    loading.value = false
  }
}

function enter(target) {
  if (target === null) return
  path.value = target
  fetchData()
}

function onRowDblclick(row) {
  row.type === 'dir' ? enter(row.path) : openFile(row)
}

// ── 文件查看 ──────────────────────────────────────────
const viewerVisible = ref(false)
const viewer = reactive({
  path: '', name: '', size: 0, mode: 'head', content: '',
  eof: true, read: 0, nextOffset: 0, loading: false,
})
const viewerHtml = computed(() => ansiToHtml(viewer.content))

function openFile(row) {
  viewer.path = row.path
  viewer.name = row.name
  // 执行日志多为追加写，默认看末尾；现场文件很小，从头读即可
  viewer.mode = row.name.endsWith('.log') ? 'tail' : 'head'
  viewerVisible.value = true
  reload()
}

async function reload() {
  viewer.content = ''
  viewer.read = 0
  await load(viewer.mode === 'tail' ? { tail: 'true' } : { offset: 0 })
}

async function loadMore() {
  await load({ offset: viewer.nextOffset })
}

async function load(params) {
  viewer.loading = true
  try {
    const res = await getLogFileContent({ path: viewer.path, ...params })
    const data = res.data || {}
    viewer.content += data.content || ''
    viewer.size = data.size || 0
    viewer.eof = !!data.eof
    viewer.read += data.length || 0
    viewer.nextOffset = (data.offset || 0) + (data.length || 0)
  } finally {
    viewer.loading = false
  }
}

async function download(row) {
  const blob = await downloadLogFile(row.path)
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = row.path.replace(/\//g, '_')
  a.click()
  URL.revokeObjectURL(url)
  ElMessage.success(`已下载 ${row.name}`)
}

function formatSize(bytes) {
  if (bytes === null || bytes === undefined) return '—'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

function formatTime(iso) {
  return iso ? iso.replace('T', ' ').slice(0, 19) : '—'
}

onMounted(fetchData)
</script>

<style scoped>
.toolbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; gap: 12px; }
.toolbar-left { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.crumb { color: #409eff; cursor: pointer; }
.row-icon { margin-right: 6px; vertical-align: -2px; color: #909399; }
.unset { color: #c0c4cc; }
.muted { color: #909399; font-size: 12px; }
code { background: #f5f7fa; padding: 1px 6px; border-radius: 3px; font-size: 12px; }
.viewer-bar { display: flex; align-items: center; gap: 12px; margin-bottom: 8px; }
.viewer-body {
  margin: 0; padding: 12px; max-height: 62vh; overflow: auto; background: #1e1e1e; color: #d4d4d4;
  border-radius: 4px; font-size: 12px; line-height: 1.5; white-space: pre-wrap; word-break: break-all;
}
</style>
