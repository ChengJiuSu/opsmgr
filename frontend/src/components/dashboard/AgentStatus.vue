<template>
  <div class="agents">
    <div class="summary">
      <el-tag size="small" type="success">在线 {{ agents.online }}</el-tag>
      <el-tag size="small" :type="agents.offline ? 'danger' : 'info'">离线 {{ agents.offline }}</el-tag>
    </div>
    <div v-for="n in agents.nodes" :key="n.id" class="row" @click="router.push('/system/agents')">
      <span class="dot" :class="n.status" />
      <span class="name">
        {{ n.name }}
        <el-tag v-if="!n.enabled" size="small" type="info">已禁用</el-tag>
      </span>
      <!-- 心跳快照里的负载/磁盘：agent 上报什么就显示什么，取不到就不占位 -->
      <span v-if="n.load1 != null" class="metric">load {{ n.load1 }}</span>
      <span v-if="n.disk_free_pct != null" class="metric" :class="{ warn: n.disk_free_pct < 15 }">
        磁盘余 {{ n.disk_free_pct }}%
      </span>
      <span v-if="n.running_jobs" class="metric">{{ n.running_jobs }} 个执行中</span>
    </div>
    <el-empty v-if="!agents.nodes.length" description="没有 Agent 节点" :image-size="60" />
  </div>
</template>

<script setup>
import { useRouter } from 'vue-router'

defineProps({
  agents: { type: Object, default: () => ({ online: 0, offline: 0, nodes: [] }) },
})

const router = useRouter()
</script>

<style scoped>
.summary { display: flex; gap: 8px; margin-bottom: 8px; }
.row {
  display: flex; align-items: center; gap: 10px;
  padding: 6px 2px; border-bottom: 1px solid #f2f6fc; font-size: 13px; cursor: pointer;
}
.row:last-child { border-bottom: none; }
.row:hover .name { color: #409eff; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: #f56c6c; flex: none; }
.dot.online { background: #67c23a; }
.name { flex: 1; display: flex; align-items: center; gap: 6px; color: #303133; }
.metric { flex: none; color: #909399; font-size: 12px; }
.metric.warn { color: #e6a23c; }
</style>
