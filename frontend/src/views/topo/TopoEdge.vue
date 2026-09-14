<!--
  自定义连线。三件事：右键点得到、关系读得出、链路跟得动。

  一、点得到（原来就有的理由）
  vue-flow 默认连线有两处点不上：可见描边只有 1px，右键要正好压在曲线上；关系文字由
  `.vue-flow__edge-labels` 层渲染，那层 CSS 是 `pointer-events: none` 且在连线的 <g>
  之外——右键点文字既不触发 edgeContextMenu，也传不到连线自己身上。而人本能会去点那
  两个字：线又细又弯，文字才是这条线唯一像「把手」的东西。所以路径带一条 30px 的透明
  交互描边，标签自己打开 pointer-events。

  二、读得出
  关系原来只靠中间那两个字区分，线全是同色同粗：图一密，得挨个凑近读标签。现在颜色 +
  线型 + 箭头三重编码（口径在 edgeStyles.js），远看就能分出这是解析还是回源。
  箭头两处：端点一个，曲线正中再一个——端点那个挨着节点卡片，几条线挤在同一个连接点时
  叠成一小撮，光靠它得把视线顺着曲线爬到头才知道往哪儿流。
  走线仍是贝塞尔曲线：正交折线在这张图上更糟——节点是四向出线的自由布局不是分层电路，
  折线会沿着同一条水平/垂直轴大段重叠，两条线压成一条；曲线各自的弧度天然把它们错开。

  三、看得见方向
  进图就有流动动画，不用先点一下。这张图是用来回答「流量往哪儿走」的，方向应该是
  第一眼的信息而不是一次交互的奖励。形式上是**每条线一个圆点沿线走**，不是铺满整条线的
  重复虚线——后者几十条一起爬像毛毛虫，看久了不适（见 edgeStyles.js 的 FLOW_DASH）。
  代价在性能上处理（见下面 flowing 的注释）。

  四、末端不再糊成一坨
  多条线汇聚到同一个连接点时，箭头精确重合，只看得见最后画的那条。所以末端按画布算好
  的扇形偏移各让开一点（fanOffset），左右进偏 Y、上下进偏 X。

  高亮态（点中或悬停某个节点后，经过它的那整条链路）由画布 provide 下来（一个共享的 Set，这里只做
  O(1) 的 has 判定）——100+ 条边时绝不能让每条线自己去遍历全图。
-->
<template>
  <BaseEdge
    :id="id"
    :path="midVertexPath"
    :marker-end="markerEnd"
    :style="edgeStyle"
    :interaction-width="30"
  />
  <!-- 沿线走的圆点。v-if 而不是 CSS 关动画：没被点亮的线连 DOM 都不生成。
       100 条边全程跑动画会让浏览器一直重绘，风扇转起来而信息量为零。
       pathLength 把这条路径的「全长」归一化成 100，dasharray 于是按百分比算，
       每条线无论长短都只有一个点（见 edgeStyles.js 的 FLOW_DASH）。
       驼峰不能写成 path-length：SVG 属性名区分大小写，短横线形式浏览器不认 -->
  <path
    v-if="flowing"
    :d="path[0]"
    class="edge-flow"
    :pathLength="FLOW_PATH_LENGTH"
    :style="flowStyle"
  />
  <EdgeLabelRenderer>
    <div
      v-if="label"
      class="edge-label"
      :class="{ selected, dim: tone === 'dim', transient: tone === 'dim' && !sticky }"
      :style="labelStyle"
      :title="`${label}（右键改关系 / 删除）`"
      @click.stop="selectEdge?.(id)"
      @contextmenu.prevent.stop="openEdgeMenu?.($event, id)"
    >{{ label }}</div>
  </EdgeLabelRenderer>
</template>

<script setup>
import { computed, inject } from 'vue'
import { BaseEdge, EdgeLabelRenderer, getBezierPath, Position } from '@vue-flow/core'
import {
  relationStyle, relationDash, relationMarker,
  EDGE_WIDTH, EDGE_OPACITY, EDGE_CURVATURE,
  FLOW_DASH, FLOW_PERIOD, FLOW_PATH_LENGTH, FLOW_DURATION, prefersReducedMotion,
  midArrowId,
} from './edgeStyles'

const props = defineProps({
  id: { type: String, required: true },
  sourceX: { type: Number, required: true },
  sourceY: { type: Number, required: true },
  targetX: { type: Number, required: true },
  targetY: { type: Number, required: true },
  sourcePosition: { type: String, default: 'right' },
  targetPosition: { type: String, default: 'left' },
  label: { type: [String, Object], default: '' },
  style: { type: Object, default: () => ({}) },
  selected: { type: Boolean, default: false },
  data: { type: Object, default: () => ({}) },
})

// 画布提供：右键开菜单 / 左键选中。连线组件只管把事件递上去
const openEdgeMenu = inject('topoOpenEdgeMenu', null)
const selectEdge = inject('topoSelectEdge', null)
// 画布提供：链路高亮集合与末端扇形偏移。都是画布那边算好的现成结果，
// 这里只查不算——每条线各算一遍等于 O(E²)
const highlight = inject('topoHighlight', null)
const fanOffsets = inject('topoFanOffsets', null)
//: 画布提供：全图流动动画开不开（顶部开关 + 边数阈值自动降级，见 edgeStyles.js）
const flowAll = inject('topoFlowAll', null)

const relation = computed(() => props.data?.relation || 'custom')

/** 三档：base（常态）/ hl（高亮链路）/ dim（被压暗的其余边）。 */
const tone = computed(() => {
  const state = highlight?.value
  if (!state?.active) return 'base'
  return state.edges.has(props.id) ? 'hl' : 'dim'
})

//: 高亮是点出来的（一直亮着）还是悬停出来的（一挪就没）。只影响被压暗那些线的
//: 标签还接不接点击：点击锁定时得接——人正要点别处；悬停时不接——鼠标只是路过
const sticky = computed(() => highlight?.value?.sticky === true)

/*
  跑不跑流动动画。三条规则，顺序要紧：

  1. 被压暗的线**一定不跑**。这是高亮省性能的关键——点中/悬停时全图只剩一条链路在动，
     其余几十上百条当场停下；同时也是视觉上的：0.15 透明度的线还在爬，那是干扰不是信息。
  2. 选中或在高亮链路上的线**一定跑**，哪怕全图动画被关掉或被阈值降级了——
     用户刚点了它，得让它从图里跳出来。
  3. 其余情况跟随全图开关。
*/
const flowing = computed(() => {
  // 系统开了减少动态：整条流动路径都不渲染，底线也就不会被压成淡轨道（见 edgeStyles.js）
  if (prefersReducedMotion.value) return false
  if (tone.value === 'dim') return false
  if (props.selected || tone.value === 'hl') return true
  return flowAll?.value ?? false
})

//: 末端偏移。左右进偏 Y、上下进偏 X：偏错轴的话线会顺着自己的方向缩短一截，
//: 看着像没连上
const fanOffset = computed(() => fanOffsets?.value?.get(props.id) || 0)
const targetPoint = computed(() => {
  const off = fanOffset.value
  if (!off) return { x: props.targetX, y: props.targetY }
  const vertical = props.targetPosition === Position.Top
    || props.targetPosition === Position.Bottom
  return vertical
    ? { x: props.targetX + off, y: props.targetY }
    : { x: props.targetX, y: props.targetY + off }
})

// [路径, 标签X, 标签Y, 偏移X, 偏移Y]
const path = computed(() => getBezierPath({
  sourceX: props.sourceX,
  sourceY: props.sourceY,
  sourcePosition: props.sourcePosition,
  targetX: targetPoint.value.x,
  targetY: targetPoint.value.y,
  targetPosition: props.targetPosition,
  curvature: EDGE_CURVATURE,
}))

/*
  把一条三次贝塞尔从正中劈成两条，好让中点成为一个真正的顶点——marker-mid 只认顶点，
  曲线中间没有顶点就挂不上箭头。

  用德卡斯特里奥分割（t=0.5 就是连取中点），结果与原曲线**逐点重合**，图形一点不变，
  只是多了个可以挂东西的顶点。

  控制点是从 getBezierPath 吐出的 `d` 字符串里解析的，不是照着 vue-flow 的曲率公式
  再算一遍：公式属于它的内部实现，跟着版本变，抄过来迟早对不上，而「一条 M + C」的
  输出形状是稳定的。解析不出 8 个数就原样返回——没有中段箭头，但线绝不会画错。
*/
const CUBIC_NUMBERS = /-?\d+(?:\.\d+)?/g
const round = (n) => Math.round(n * 1000) / 1000

function splitCubicAtMid(d) {
  const nums = d.match(CUBIC_NUMBERS)?.map(Number)
  if (!nums || nums.length !== 8 || nums.some(Number.isNaN)) return d
  const [x0, y0, x1, y1, x2, y2, x3, y3] = nums
  const mid = (ax, ay, bx, by) => [(ax + bx) / 2, (ay + by) / 2]
  const [ax, ay] = mid(x0, y0, x1, y1)
  const [bx, by] = mid(x1, y1, x2, y2)
  const [cx, cy] = mid(x2, y2, x3, y3)
  const [dx, dy] = mid(ax, ay, bx, by)
  const [ex, ey] = mid(bx, by, cx, cy)
  //: 曲线正中，两段的公共顶点，中段箭头就挂在这儿
  const [mx, my] = mid(dx, dy, ex, ey)
  return `M${round(x0)},${round(y0)} C${round(ax)},${round(ay)} ${round(dx)},${round(dy)} `
    + `${round(mx)},${round(my)} C${round(ex)},${round(ey)} ${round(cx)},${round(cy)} `
    + `${round(x3)},${round(y3)}`
}

const midVertexPath = computed(() => splitCubicAtMid(path.value[0]))

const edgeStyle = computed(() => {
  const { color } = relationStyle(relation.value)
  const hl = tone.value === 'hl' || props.selected
  return {
    // 外部传进来的 style 放最前：画布若要覆盖（比如将来做「按环境着色」）仍然能覆盖
    ...props.style,
    stroke: color,
    strokeWidth: hl ? EDGE_WIDTH.highlight : EDGE_WIDTH.base,
    strokeDasharray: relationDash(relation.value),
    // opacity 作用在 <path> 上会连带作用到它的 marker——箭头跟着一起暗，
    // 不会出现「线暗了箭头还亮着」的怪相
    opacity: EDGE_OPACITY[tone.value === 'hl' ? 'highlight' : tone.value],
    // 跑流动时底线退成淡轨道，圆点才有东西可对比——这是「看不出在动」的正解。
    // 压的是 stroke-opacity 不是 opacity：后者会把箭头一起压淡（见 edgeStyles.js）
    strokeOpacity: flowing.value ? EDGE_OPACITY.track : 1,
    // 中段箭头。marker-mid 是 CSS 属性，挂在曲线正中那个顶点上（见 splitCubicAtMid），
    // 方向由浏览器按切线自动算。默认没有流动动画，方向就靠它和端点箭头交代
    markerMid: `url(#${midArrowId(relation.value)})`,
  }
})

//: 箭头按关系着色。对象形式的 markerEnd 由 vue-flow 去重生成 defs，
//: 所以最多 7 个 marker 定义，与边数无关
const markerEnd = computed(() => relationMarker(relation.value))

const flowStyle = computed(() => ({
  stroke: relationStyle(relation.value).color,
  // 圆点的直径。dash 段近乎零长度，圆头描边把它渲染成一个圆，所以「描边宽度」就是
  // 点的大小（见 edgeStyles.js 的 FLOW_DASH）
  strokeWidth: EDGE_WIDTH.dot,
  strokeDasharray: FLOW_DASH,
  animationDuration: FLOW_DURATION,
  // 位移量交给 CSS 变量：写死在 @keyframes 里就和 FLOW_DASH 分了家，改一处忘一处，
  // 周期对不上每轮末尾会跳一下。单位是 px，在 pathLength 归一化后 100px = 走完全程
  '--flow-period': `${FLOW_PERIOD}px`,
}))

const labelStyle = computed(() => {
  const { color } = relationStyle(relation.value)
  return {
    transform: `translate(-50%, -50%) translate(${path.value[1]}px, ${path.value[2]}px)`,
    // 标签跟着线着色，顺带就是一份贴在图上的图例
    '--rel-color': color,
  }
})
</script>

<style scoped>
/* 沿线走的圆点。注意它仍然是一个 stroke-dashoffset 动画，不是 animateMotion 沿路径跑的
   <circle>——后者每条线要多一个 SMIL 节点和一次路径采样，而 dashoffset 只是一个属性动画，
   路径变了也不用重新采样。圆点从「零长度 dash + 圆头」来（见 edgeStyles.js）。 */
.edge-flow {
  fill: none;
  pointer-events: none;
  /* 圆头是圆点的来源，不是装饰：零长度的 dash 段靠它渲染成一个圆。改成 butt 会
     什么都画不出来 */
  stroke-linecap: round;
  animation-name: edge-ants;
  animation-timing-function: linear;
  animation-iteration-count: infinite;
}
@keyframes edge-ants {
  /* 位移量 = 一个 dash 周期，由 FLOW_DASH 算出来经 --flow-period 传进来 */
  to { stroke-dashoffset: calc(-1 * var(--flow-period)); }
}
/* 减少动态的处理不在这儿：只关动画会留下「淡轨道 + 一截静止亮段」，比动着还难读。
   改在 JS 里连这条路径一起不渲染（见 edgeStyles.js 的 prefersReducedMotion） */

.edge-label {
  position: absolute;
  /* 所在的 .vue-flow__edge-labels 层是 pointer-events: none，这里必须自己打开，
     否则右键点在文字上什么都不会发生 */
  pointer-events: all;
  padding: 1px 6px;
  font-size: 11px;
  line-height: 16px;
  color: var(--rel-color, #606266);
  background: #fff;
  border: 1px solid var(--rel-color, #dcdfe6);
  border-radius: 8px;
  cursor: pointer;
  white-space: nowrap;
  user-select: none;
  transition: opacity 0.15s ease;
}
/* 被压暗的线，标签也得跟着暗：白底文字不暗下去的话，图上会剩一片浮着的字，
   比线本身还抢眼 */
.edge-label.dim { opacity: 0.15; }
/* 悬停预览期间才关掉点击：鼠标正贴着节点划过，暗着的标签不该被误点。点击锁定时
   保持可点，否则人被锁在当前选择里，只能先点空白才能换一条线看 */
.edge-label.dim.transient { pointer-events: none; }
.edge-label:hover { border-color: #409eff; color: #409eff; }
.edge-label.selected { border-color: #409eff; color: #409eff; background: #ecf5ff; }
</style>
