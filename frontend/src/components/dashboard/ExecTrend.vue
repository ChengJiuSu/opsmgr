<template>
  <div class="wrap">
    <div class="legend">
      <span><i class="dot success" />成功 {{ totals.success }}</span>
      <span><i class="dot failed" />失败 {{ totals.failed }}</span>
      <span v-if="totals.running"><i class="dot running" />未结束 {{ totals.running }}</span>
    </div>
    <div class="trend">
      <div v-for="d in days" :key="d.date" class="col" @click="router.push('/task/executions')">
        <div class="bar-wrap">
          <!-- 一天一根堆叠柱：成功在下、失败在上，一眼看出这周稳不稳 -->
          <div class="stack" :style="{ height: barHeight(d) }" :title="tip(d)">
            <div v-if="d.failed" class="seg failed" :style="{ flex: d.failed }" />
            <div v-if="d.running" class="seg running" :style="{ flex: d.running }" />
            <div v-if="d.success" class="seg success" :style="{ flex: d.success }" />
          </div>
        </div>
        <div class="label">{{ d.date.slice(5) }}</div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useRouter } from 'vue-router'

const props = defineProps({
  days: { type: Array, default: () => [] },
})

const router = useRouter()

const total = (d) => d.success + d.failed + d.running

const totals = computed(() => props.days.reduce(
  (acc, d) => ({
    success: acc.success + d.success,
    failed: acc.failed + d.failed,
    running: acc.running + d.running,
  }),
  { success: 0, failed: 0, running: 0 },
))

const maxCount = computed(() => Math.max(1, ...props.days.map(total)))

// 一次执行都没有的那天留 2% 的底，柱子完全消失会让人以为图没加载出来
const barHeight = (d) => `${Math.round((total(d) / maxCount.value) * 100) || 2}%`

const tip = (d) => `成功 ${d.success} · 失败 ${d.failed}`
  + (d.running ? ` · 未结束 ${d.running}` : '')
</script>

<style scoped>
.wrap { display: flex; flex-direction: column; height: 100%; }
.legend { display: flex; gap: 14px; font-size: 12px; color: #909399; margin-bottom: 6px; }
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 2px; margin-right: 5px; }
.dot.success { background: #67c23a; }
.dot.failed { background: #f56c6c; }
.dot.running { background: #e6a23c; }

.trend { display: flex; align-items: flex-end; flex: 1; min-height: 240px; gap: 8px; padding: 8px 0; }
.col { flex: 1; display: flex; flex-direction: column; align-items: center; height: 100%; cursor: pointer; }
.bar-wrap { flex: 1; width: 100%; display: flex; align-items: flex-end; justify-content: center; }
.stack {
  width: 60%; min-height: 2px; display: flex; flex-direction: column;
  border-radius: 4px 4px 0 0; overflow: hidden; transition: height 0.3s;
}
.seg.success { background: linear-gradient(180deg, #95d475, #67c23a); }
.seg.failed { background: linear-gradient(180deg, #f89898, #f56c6c); }
.seg.running { background: linear-gradient(180deg, #f3d19e, #e6a23c); }
.label { margin-top: 8px; color: #909399; font-size: 12px; }
</style>
