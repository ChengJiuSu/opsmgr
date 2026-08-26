<template>
  <!-- 任务流示意图：开始 → 各执行阶段 → 结束。相邻并行模块合并成一个阶段（竖排），
       与后端 runner._build_stages 的切分口径一致 -->
  <div class="flow-graph">
    <div class="flow-node flow-endpoint">开始</div>
    <template v-for="(stage, si) in stages" :key="si">
      <div class="flow-arrow" />
      <div class="flow-stage" :class="{ parallel: stage.length > 1 }">
        <span v-if="stage.length > 1" class="stage-tag">并行组</span>
        <div
          v-for="step in stage" :key="step.id"
          class="flow-card" :class="{ active: step.id === active, clickable, compact }"
          @click="clickable && $emit('select', step.id)"
        >
          <div class="card-head">
            <span class="card-order">{{ orderOf(step) }}</span>
            <span class="card-type">{{ TYPE_LABEL[step.task_type] || step.task_type }}</span>
            <span v-if="statusOf(step)" class="card-status" :class="`st-${statusOf(step)}`">
              {{ STATUS_LABEL[statusOf(step)] || statusOf(step) }}
            </span>
          </div>
          <div class="card-body" :title="step.name">{{ step.name }}</div>
          <div v-if="metaOf(step)" class="card-meta">{{ metaOf(step) }}</div>
        </div>
      </div>
    </template>
    <div class="flow-arrow" />
    <div class="flow-node flow-endpoint">结束</div>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  // 模块列表（任务序列化的 steps）
  steps: { type: Array, default: () => [] },
  // 当前选中模块 id（高亮）
  active: { type: [Number, String], default: null },
  clickable: { type: Boolean, default: false },
  // 模块 id → 状态（success/failed/running/skipped），执行日志场景传入
  statusMap: { type: Object, default: () => ({}) },
  // 模块 id → 附加说明（如「已选 7 台」）
  metaMap: { type: Object, default: () => ({}) },
})
defineEmits(['select'])

const TYPE_LABEL = { command: '命令', playbook: 'Playbook', method: '平台方法', pause: '暂停确认' }
const STATUS_LABEL = { success: '成功', failed: '失败', running: '执行中', skipped: '跳过' }

// 切分执行阶段：相邻 run_parallel 模块合并（pause 强制独立），阶段间严格按序
const stages = computed(() => {
  const out = []
  let buf = []
  props.steps.forEach((step) => {
    if (step.run_parallel && step.task_type !== 'pause') {
      buf.push(step)
      return
    }
    if (buf.length) {
      out.push(buf)
      buf = []
    }
    out.push([step])
  })
  if (buf.length) out.push(buf)
  return out
})

const orderOf = (step) => props.steps.findIndex((s) => s.id === step.id) + 1
// 模块多时自动收紧卡片（副标题仍保留，宽度与内边距压小），保证一屏全量可见
const compact = computed(() => props.steps.length > 8)
const statusOf = (step) => props.statusMap[step.id] || ''
const metaOf = (step) => props.metaMap[step.id] || ''
</script>

<style scoped>
/* 全量展示：节点按行回绕，不做横向滚动；模块多时卡片自动收紧 */
.flow-graph {
  display: flex; align-items: center; flex-wrap: wrap; row-gap: 12px;
  padding: 14px 12px;
  border: 1px solid #ebeef5; border-radius: 6px; background: #f7f8fa;
}
.flow-node.flow-endpoint {
  flex-shrink: 0; width: 46px; height: 46px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  background: #e4e7ed; color: #606266; font-size: 12px;
}
/* 阶段间箭头 */
.flow-arrow {
  flex-shrink: 0; width: 26px; height: 1px; background: #c0c4cc; position: relative;
}
.flow-arrow::after {
  content: ''; position: absolute; right: 0; top: -3px;
  border-left: 5px solid #c0c4cc; border-top: 3.5px solid transparent; border-bottom: 3.5px solid transparent;
}
.flow-stage { flex-shrink: 0; display: flex; flex-direction: column; gap: 6px; position: relative; }
.flow-stage.parallel {
  padding: 14px 8px 8px; border: 1px dashed #c0c4cc; border-radius: 6px; background: #fff;
}
.stage-tag {
  position: absolute; top: -9px; left: 8px; padding: 0 5px;
  background: #f7f8fa; color: #909399; font-size: 11px;
}
/* 模块卡：统一灰调，选中态靠蓝色描边区分 */
.flow-card {
  width: 158px; background: #fff; border: 1px solid #dcdfe6; border-radius: 4px;
  overflow: hidden; transition: box-shadow .15s, border-color .15s;
}
.flow-card.compact { width: 120px; }
.flow-card.compact .card-body { padding: 5px 6px 3px; font-size: 12px; }
.flow-card.compact .card-meta { padding: 0 6px 5px; font-size: 11px; }
.flow-card.clickable { cursor: pointer; }
.flow-card.clickable:hover { box-shadow: 0 2px 8px rgba(0, 0, 0, .08); }
.flow-card.active { border-color: #409eff; box-shadow: 0 0 0 2px rgba(64, 158, 255, .18); }
/* 统一灰色头部：类型只用文字区分，避免多色噪点 */
.card-head {
  display: flex; align-items: center; gap: 5px; padding: 4px 8px;
  color: #fff; font-size: 11px; background: #8a94a6;
}
.flow-card.active .card-head { background: #6b7688; }
.card-order { opacity: .85; }
.card-type { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.card-status { padding: 0 4px; border-radius: 2px; background: rgba(255, 255, 255, .25); }
.card-body {
  padding: 7px 8px 5px; font-size: 13px; color: #303133; font-weight: 600;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.card-meta {
  padding: 0 8px 7px; font-size: 12px; color: #909399;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
</style>
