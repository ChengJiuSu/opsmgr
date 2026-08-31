<template>
  <div v-if="tasks.length" class="running">
    <div v-for="t in tasks" :key="t.log_id" class="row" @click="open(t)">
      <el-icon class="is-loading spin"><Loading /></el-icon>
      <span class="name">{{ t.name }}</span>
      <span class="step" v-if="t.step_total">
        模块 {{ Math.min(t.step_done + 1, t.step_total) }}/{{ t.step_total }}
      </span>
      <span class="by">{{ t.triggered_by || '-' }}</span>
      <span class="elapsed">{{ elapsed(t) }}</span>
    </div>
  </div>
  <el-empty v-else description="当前没有执行中的任务" :image-size="60" />
</template>

<script setup>
import { Loading } from '@element-plus/icons-vue'
import { useRouter } from 'vue-router'

defineProps({
  tasks: { type: Array, default: () => [] },
})

const router = useRouter()

/** 已跑时长：后端给的 elapsed 是取数那一刻的值，页面 30s 刷一次，够用。 */
function elapsed(t) {
  const s = t.elapsed
  if (s == null) return ''
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m${s % 60}s`
  return `${Math.floor(s / 3600)}h${Math.floor((s % 3600) / 60)}m`
}

function open(t) {
  if (t.id && t.log_id) router.push(`/task/executions/${t.id}/logs/${t.log_id}`)
}
</script>

<style scoped>
.running { display: flex; flex-direction: column; }
.row {
  display: flex; align-items: center; gap: 10px;
  padding: 7px 2px; border-bottom: 1px solid #f2f6fc;
  font-size: 13px; cursor: pointer;
}
.row:last-child { border-bottom: none; }
.row:hover .name { color: #409eff; }
.spin { color: #409eff; }
.name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #303133; }
.step { flex: none; color: #606266; font-size: 12px; }
.by { flex: none; color: #909399; font-size: 12px; width: 70px; text-align: right; }
.elapsed { flex: none; color: #909399; font-size: 12px; width: 60px; text-align: right; }
</style>
