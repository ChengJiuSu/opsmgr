<template>
  <div class="assets">
    <div class="summary">
      <div class="num" @click="router.push('/assets/servers')">
        <span class="value">{{ assets.total }}</span>
        <span class="label">主机总数</span>
      </div>
      <div class="num" @click="router.push('/config/allocation')">
        <span class="value">{{ assets.services?.deployed ?? 0 }}</span>
        <span class="label">已部署服务</span>
      </div>
      <!-- 已规划未部署 = 一批等着有人去部署的机器，是这张卡里唯一有动作含义的数 -->
      <div class="num" :class="{ warn: assets.services?.planned }" @click="router.push('/config/allocation')">
        <span class="value">{{ assets.services?.planned ?? 0 }}</span>
        <span class="label">已规划待部署</span>
      </div>
      <div class="num muted" @click="router.push('/config/allocation')">
        <span class="value">{{ assets.services?.offline ?? 0 }}</span>
        <span class="label">已下线</span>
      </div>
    </div>

    <div class="dists">
      <div v-for="d in dists" :key="d.title" class="dist">
        <div class="dist-title">{{ d.title }}</div>
        <div v-for="row in d.rows" :key="row.name" class="bar-row">
          <span class="bar-name">{{ row.name }}</span>
          <div class="bar-track">
            <div class="bar-fill" :style="{ width: pct(row.count, d.rows) }" />
          </div>
          <span class="bar-count">{{ row.count }}</span>
        </div>
        <div v-if="!d.rows.length" class="empty">暂无数据</div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useRouter } from 'vue-router'

const props = defineProps({
  assets: { type: Object, default: () => ({ total: 0, services: {}, by_environment: [], by_project_group: [] }) },
})

const router = useRouter()

const dists = computed(() => [
  { title: '按环境', rows: props.assets.by_environment || [] },
  { title: '按项目组', rows: props.assets.by_project_group || [] },
])

// 条长按该组最大值归一：看的是相对多少，不是占总数的百分比
function pct(count, rows) {
  const max = Math.max(1, ...rows.map((r) => r.count))
  return `${Math.max(2, Math.round((count / max) * 100))}%`
}
</script>

<style scoped>
.summary { display: flex; gap: 8px; margin-bottom: 14px; }
.num {
  flex: 1; display: flex; flex-direction: column; gap: 4px;
  padding: 8px 10px; background: #f5f7fa; border-radius: 6px; cursor: pointer;
}
.num:hover { background: #ecf5ff; }
.num .value { font-size: 22px; font-weight: 700; color: #303133; line-height: 1; }
.num .label { color: #909399; font-size: 12px; }
.num.warn .value { color: #e6a23c; }
.num.muted .value { color: #909399; }

.dists { display: flex; gap: 20px; }
.dist { flex: 1; min-width: 0; }
.dist-title { color: #909399; font-size: 12px; margin-bottom: 6px; }
.bar-row { display: flex; align-items: center; gap: 8px; margin-bottom: 5px; }
.bar-name {
  width: 72px; flex: none; font-size: 12px; color: #606266;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.bar-track { flex: 1; height: 10px; background: #f2f6fc; border-radius: 5px; overflow: hidden; }
.bar-fill { height: 100%; background: linear-gradient(90deg, #a0cfff, #409eff); border-radius: 5px; }
.bar-count { width: 32px; flex: none; text-align: right; font-size: 12px; color: #909399; }
.empty { color: #c0c4cc; font-size: 12px; }
</style>
