<template>
  <div>
    <el-row :gutter="16" class="stat-cards">
      <el-col :span="6" v-for="card in statCards" :key="card.label">
        <el-card shadow="never" class="stat-card">
          <div class="stat-inner">
            <div class="stat-info">
              <div class="stat-value">{{ card.value }}</div>
              <div class="stat-label">{{ card.label }}</div>
            </div>
            <el-icon :size="44" :color="card.color" class="stat-icon">
              <component :is="card.icon" />
            </el-icon>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="16" style="margin-top: 16px">
      <el-col :span="12">
        <el-card header="近 7 天操作趋势" shadow="never" v-loading="loading">
          <div class="trend">
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
import { ref, onMounted, computed } from 'vue'
import { Box, Monitor, Setting, Document } from '@element-plus/icons-vue'
import { getDashboardStats } from '../api/dashboard'
import { getAuditLogs } from '../api/audit'

const loading = ref(false)
const logs = ref([])
const recentActions = ref([])

const statCards = ref([
  { label: '服务器总数', value: 0, icon: Box, color: '#409EFF' },
  { label: '运行中', value: 0, icon: Monitor, color: '#67C23A' },
  { label: '维护中', value: 0, icon: Setting, color: '#E6A23C' },
  { label: '今日操作', value: 0, icon: Document, color: '#F56C6C' },
])

const maxCount = computed(() => Math.max(1, ...recentActions.value.map((d) => d.count)))
const barHeight = (count) => `${Math.round((count / maxCount.value) * 100) || 2}%`

const methodType = (m) => ({ GET: 'info', POST: 'success', PUT: 'warning', PATCH: 'warning', DELETE: 'danger' }[m] || 'info')
const formatTime = (t) => (t ? t.slice(5, 16).replace('T', ' ') : '')

onMounted(async () => {
  loading.value = true
  try {
    const [statsRes, logRes] = await Promise.allSettled([
      getDashboardStats(),
      getAuditLogs({ page_size: 10 }),
    ])
    if (statsRes.status === 'fulfilled') {
      const d = statsRes.value.data || {}
      statCards.value[0].value = d.servers?.total ?? 0
      statCards.value[1].value = d.servers?.running ?? 0
      statCards.value[2].value = d.servers?.maintenance ?? 0
      statCards.value[3].value = d.audit?.today ?? 0
      recentActions.value = d.recent_actions || []
    }
    if (logRes.status === 'fulfilled') {
      logs.value = logRes.value.data?.results || []
    }
  } finally {
    loading.value = false
  }
})
</script>

<style scoped>
.stat-card { border-radius: 8px; }
.stat-inner { display: flex; align-items: center; justify-content: space-between; padding: 8px 4px; }
.stat-value { font-size: 32px; font-weight: 700; color: #303133; line-height: 1; }
.stat-label { margin-top: 8px; color: #909399; font-size: 14px; }
.stat-icon { opacity: 0.15; }

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
