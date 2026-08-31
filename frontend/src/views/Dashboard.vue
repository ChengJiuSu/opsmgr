<template>
  <div>
    <div class="page-bar">
      <span class="refreshed">{{ refreshedLabel }}</span>
      <el-button link :icon="Refresh" @click="fetchOverview">刷新</el-button>
      <!-- 显示哪些区块是各人的习惯（只看告警 / 只看执行），存本地，不占后端字段 -->
      <el-popover placement="bottom-end" :width="180" trigger="click">
        <template #reference>
          <el-button link :icon="SetUp">自定义</el-button>
        </template>
        <el-checkbox
          v-for="s in SECTIONS"
          :key="s.key"
          :model-value="visibleSections.includes(s.key)"
          class="section-toggle"
          @change="toggleSection(s.key)"
        >{{ s.label }}</el-checkbox>
      </el-popover>
    </div>

    <!-- 值班区：出事没？有没有等我的？块的有无由后端按权限裁剪 -->
    <el-card v-if="shown('duty')" shadow="never" v-loading="overviewLoading" class="duty-card">
      <template #header>
        <div class="card-head"><span>值班状态</span></div>
      </template>

      <div v-if="!alerts.length && !todos.length" class="all-clear">
        <el-icon><CircleCheckFilled /></el-icon>
        当前无异常，也没有等待处理的事项
      </div>
      <template v-else>
        <DutyBlocks v-if="alerts.length" :blocks="alerts" />
        <el-divider v-if="alerts.length && todos.length" />
        <div v-if="todos.length">
          <div class="section-label">等待处理</div>
          <DutyBlocks :blocks="todos" />
        </div>
      </template>
    </el-card>

    <!-- 运行态：现在在跑什么 -->
    <el-row v-if="shown('running') && (runningVisible || agentsVisible)" :gutter="16" class="row-gap">
      <el-col :span="runningVisible && agentsVisible ? 14 : 24" v-if="runningVisible">
        <el-card shadow="never" header="执行中的任务">
          <RunningTasks :tasks="running.tasks" />
        </el-card>
      </el-col>
      <el-col :span="runningVisible ? 10 : 24" v-if="agentsVisible">
        <el-card shadow="never" header="Agent 节点">
          <AgentStatus :agents="agents" />
        </el-card>
      </el-col>
    </el-row>

    <!-- 资产盘子：主机分布与服务规划三态 + 各维度当前版本 -->
    <el-row v-if="shown('assets') && (assetsVisible || artifactsVisible)" :gutter="16" class="row-gap">
      <el-col :span="assetsVisible && artifactsVisible ? 13 : 24" v-if="assetsVisible">
        <el-card shadow="never" header="资产分布">
          <AssetDistribution :assets="assets" />
        </el-card>
      </el-col>
      <el-col :span="assetsVisible ? 11 : 24" v-if="artifactsVisible">
        <el-card shadow="never" header="各环境当前版本">
          <LatestArtifacts :items="artifacts.items" />
        </el-card>
      </el-col>
    </el-row>

    <el-row v-if="shown('activity')" :gutter="16" class="row-gap">
      <el-col :span="12">
        <el-card shadow="never" v-loading="loading">
          <template #header>
            <div class="card-head">
              <span>{{ trendVisible ? '近 7 天执行' : '近 7 天写操作趋势' }}</span>
              <span class="refreshed">今日操作 {{ todayActions }} 次</span>
            </div>
          </template>
          <!-- 有 exec 权限就画执行成败；只有审计权限的人回退到原来的写操作条数 -->
          <ExecTrend v-if="trendVisible" :days="trend.days" />
          <div v-else class="trend">
            <div v-for="d in recentActions" :key="d.date" class="trend-col">
              <div class="trend-bar-wrap">
                <div
                  class="trend-bar"
                  :style="{ height: barHeight(d.count) }"
                  :title="`${d.count} 次操作`"
                >
                  <span v-if="d.count" class="trend-count">{{ d.count }}</span>
                </div>
              </div>
              <div class="trend-label">{{ d.date.slice(5) }}</div>
            </div>
          </div>
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card header="最近操作记录" shadow="never">
          <el-table :data="logs" size="small" max-height="300">
            <el-table-column prop="username" label="用户" width="90" />
            <el-table-column prop="method" label="方法" width="70">
              <template #default="{ row }">
                <el-tag size="small" :type="methodType(row.method)">{{ row.method }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="path" label="路径" show-overflow-tooltip />
            <el-table-column prop="created_at" label="时间" width="100">
              <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted, computed } from 'vue'
import { Refresh, SetUp, CircleCheckFilled } from '@element-plus/icons-vue'
import DutyBlocks from '../components/dashboard/DutyBlocks.vue'
import RunningTasks from '../components/dashboard/RunningTasks.vue'
import AgentStatus from '../components/dashboard/AgentStatus.vue'
import AssetDistribution from '../components/dashboard/AssetDistribution.vue'
import LatestArtifacts from '../components/dashboard/LatestArtifacts.vue'
import ExecTrend from '../components/dashboard/ExecTrend.vue'
import { useAuthStore } from '../stores/auth'
import { getDashboardStats, getDashboardOverview } from '../api/dashboard'
import { getAuditLogs } from '../api/audit'

const loading = ref(false)
const logs = ref([])
const recentActions = ref([])

// ── 值班区 ──────────────────────────────────────────
const overviewLoading = ref(false)
const alerts = ref([])
const todos = ref([])
const running = ref({ visible: false, tasks: [] })
const agents = ref({ visible: false, online: 0, offline: 0, nodes: [] })
const assets = ref({ visible: false, total: 0, services: {}, by_environment: [], by_project_group: [] })
const artifacts = ref({ visible: false, items: [] })
const trend = ref({ visible: false, days: [] })
const refreshedAt = ref(null)

const runningVisible = computed(() => running.value.visible)
const agentsVisible = computed(() => agents.value.visible)
const assetsVisible = computed(() => assets.value.visible)
const artifactsVisible = computed(() => artifacts.value.visible)
const trendVisible = computed(() => trend.value.visible && trend.value.days.length > 0)
const refreshedLabel = computed(() =>
  refreshedAt.value ? `更新于 ${refreshedAt.value.toTimeString().slice(0, 8)}` : '',
)

async function fetchOverview() {
  overviewLoading.value = true
  try {
    const res = await getDashboardOverview()
    const d = res.data || {}
    alerts.value = d.alerts || []
    todos.value = d.todos || []
    running.value = d.running || { visible: false, tasks: [] }
    agents.value = d.agents || { visible: false, online: 0, offline: 0, nodes: [] }
    if (d.assets) assets.value = d.assets
    if (d.artifacts) artifacts.value = d.artifacts
    if (d.trend) trend.value = d.trend
    refreshedAt.value = new Date()
  } finally {
    overviewLoading.value = false
  }
}

// 30s 轮询：值班页要能自己变红，不能等人按 F5。标签页不在前台时跳过——
// 后台标签开一整天等于白刷几千次，回到前台的第一次刷新会立刻补上
let timer = null
function tick() {
  if (!document.hidden) fetchOverview()
}
function onVisible() {
  if (!document.hidden) fetchOverview()
}

// ── 区块显隐（每个人看这页的重点不同，存本地）──────────
const SECTIONS = [
  { key: 'duty', label: '值班状态' },
  { key: 'running', label: '运行态' },
  { key: 'assets', label: '资产盘子' },
  { key: 'activity', label: '趋势与操作记录' },
]
const authStore = useAuthStore()
// 按用户名分键：同一台机器上换个人登录，不该继承上一个人的布局
const storageKey = `dashboard_sections_${authStore.username || 'anonymous'}`
const visibleSections = ref(loadSections())

function loadSections() {
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey) || 'null')
    // 只认已登记的 key：版本迭代删掉某个区块后，旧记录里的残留不该影响判断
    if (Array.isArray(saved)) return saved.filter((k) => SECTIONS.some((s) => s.key === k))
  } catch {
    // 存储被清空或写坏时按默认全显示
  }
  return SECTIONS.map((s) => s.key)
}

const shown = (key) => visibleSections.value.includes(key)

function toggleSection(key) {
  visibleSections.value = shown(key)
    ? visibleSections.value.filter((k) => k !== key)
    : [...visibleSections.value, key]
  localStorage.setItem(storageKey, JSON.stringify(visibleSections.value))
}

// ── 活动流 ──────────────────────────────────────────
const todayActions = ref(0)

const maxCount = computed(() => Math.max(1, ...recentActions.value.map((d) => d.count)))
const barHeight = (count) => `${Math.round((count / maxCount.value) * 100) || 2}%`

const methodType = (m) => ({ GET: 'info', POST: 'success', PUT: 'warning', PATCH: 'warning', DELETE: 'danger' }[m] || 'info')
const formatTime = (t) => (t ? t.slice(5, 16).replace('T', ' ') : '')

onMounted(async () => {
  fetchOverview()
  timer = setInterval(tick, 30000)
  document.addEventListener('visibilitychange', onVisible)

  loading.value = true
  try {
    const [statsRes, logRes] = await Promise.allSettled([
      getDashboardStats(),
      getAuditLogs({ page_size: 10 }),
    ])
    if (statsRes.status === 'fulfilled') {
      const d = statsRes.value.data || {}
      todayActions.value = d.audit?.today ?? 0
      recentActions.value = d.recent_actions || []
    }
    if (logRes.status === 'fulfilled') {
      logs.value = logRes.value.data?.results || []
    }
  } finally {
    loading.value = false
  }
})

onUnmounted(() => {
  clearInterval(timer)
  document.removeEventListener('visibilitychange', onVisible)
})
</script>

<style scoped>
.duty-card { border-radius: 8px; }
.card-head { display: flex; justify-content: space-between; align-items: center; }
.head-right { display: flex; align-items: center; gap: 10px; }
.refreshed { color: #c0c4cc; font-size: 12px; }
.all-clear { display: flex; align-items: center; gap: 8px; color: #67c23a; font-size: 14px; padding: 4px 0; }
.section-label { color: #909399; font-size: 12px; margin-bottom: 8px; }
.row-gap { margin-top: 16px; }
/* 同一行的两张卡拉齐：内容多少不同的两块并排时，卡片下沿错开一截很扎眼 */
.row-gap :deep(.el-col) { display: flex; }
.row-gap :deep(.el-card) { width: 100%; display: flex; flex-direction: column; }
.row-gap :deep(.el-card__body) { flex: 1; }

.page-bar { display: flex; align-items: center; justify-content: flex-end; gap: 10px; margin-bottom: 12px; }
.section-toggle { display: block; margin-right: 0; }

.trend { display: flex; align-items: flex-end; height: 280px; gap: 8px; padding: 8px 0; }
.trend-col { flex: 1; display: flex; flex-direction: column; align-items: center; height: 100%; }
.trend-bar-wrap { flex: 1; width: 100%; display: flex; align-items: flex-end; justify-content: center; }
.trend-bar {
  width: 60%;
  min-height: 2px;
  background: linear-gradient(180deg, #66b1ff, #409eff);
  border-radius: 4px 4px 0 0;
  display: flex;
  justify-content: center;
  align-items: flex-start;
  transition: height 0.3s;
}
.trend-count { color: #fff; font-size: 12px; margin-top: 2px; }
.trend-label { margin-top: 8px; color: #909399; font-size: 12px; }
</style>
