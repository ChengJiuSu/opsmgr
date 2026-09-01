<!--
  自定义连线：把「右键改关系」真正做得能点到。

  vue-flow 默认连线有两个点不上的地方：

  1. 可见描边只有 1px，右键要正好压在那条曲线上；
  2. 关系文字（「解析」「源站」）由 `.vue-flow__edge-labels` 层渲染，那层的 CSS 是
     `pointer-events: none`，而且它在连线的 <g> 之外——右键点在文字上既不会触发
     vue-flow 的 edgeContextMenu，也传不到连线自己身上。

  而人本能会去点那两个字：曲线又细又弯，文字才是这条线唯一像「把手」的东西。

  所以这里自己渲染：路径给一条 30px 的透明交互描边（BaseEdge 的 interactionWidth），
  标签自己带 pointer-events 与 contextmenu/click 处理，两条路通到同一个菜单。
  菜单函数由画布 provide 下来——连线组件不该知道菜单长什么样。
-->
<template>
  <BaseEdge
    :id="id"
    :path="path[0]"
    :marker-end="markerEnd"
    :style="style"
    :interaction-width="30"
  />
  <EdgeLabelRenderer>
    <div
      v-if="label"
      class="edge-label"
      :class="{ selected }"
      :style="labelStyle"
      :title="`${label}（右键改关系 / 删除）`"
      @click.stop="selectEdge?.(id)"
      @contextmenu.prevent.stop="openEdgeMenu?.($event, id)"
    >{{ label }}</div>
  </EdgeLabelRenderer>
</template>

<script setup>
import { computed, inject } from 'vue'
import { BaseEdge, EdgeLabelRenderer, getBezierPath } from '@vue-flow/core'

const props = defineProps({
  id: { type: String, required: true },
  sourceX: { type: Number, required: true },
  sourceY: { type: Number, required: true },
  targetX: { type: Number, required: true },
  targetY: { type: Number, required: true },
  sourcePosition: { type: String, default: 'right' },
  targetPosition: { type: String, default: 'left' },
  label: { type: [String, Object], default: '' },
  markerEnd: { type: String, default: '' },
  style: { type: Object, default: () => ({}) },
  selected: { type: Boolean, default: false },
  data: { type: Object, default: () => ({}) },
})

// 画布提供：右键开菜单 / 左键选中。连线组件只管把事件递上去
const openEdgeMenu = inject('topoOpenEdgeMenu', null)
const selectEdge = inject('topoSelectEdge', null)

// [路径, 标签X, 标签Y, 偏移X, 偏移Y]
const path = computed(() => getBezierPath({
  sourceX: props.sourceX,
  sourceY: props.sourceY,
  sourcePosition: props.sourcePosition,
  targetX: props.targetX,
  targetY: props.targetY,
  targetPosition: props.targetPosition,
}))

const labelStyle = computed(() => ({
  transform: `translate(-50%, -50%) translate(${path.value[1]}px, ${path.value[2]}px)`,
}))
</script>

<style scoped>
.edge-label {
  position: absolute;
  /* 所在的 .vue-flow__edge-labels 层是 pointer-events: none，这里必须自己打开，
     否则右键点在文字上什么都不会发生 */
  pointer-events: all;
  padding: 1px 6px;
  font-size: 11px;
  line-height: 16px;
  color: #606266;
  background: #fff;
  border: 1px solid #dcdfe6;
  border-radius: 8px;
  cursor: pointer;
  white-space: nowrap;
  user-select: none;
}
.edge-label:hover { border-color: #409eff; color: #409eff; }
.edge-label.selected { border-color: #409eff; color: #409eff; background: #ecf5ff; }
</style>
