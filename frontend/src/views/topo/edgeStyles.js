/**
 * 连线的视觉口径：颜色 / 线型 / 箭头 / 高亮分档，全部集中在这里。
 *
 * 抽出来不是为了好看，是因为这套口径有三个地方要读得一模一样：
 * 画布上的线（TopoEdge.vue）、右侧详情与右键菜单里的关系项、图例。
 * 硬编码在组件里的结果是改一处忘两处，图上是蓝线、图例是灰块。
 *
 * 关系取值与后端 `apps/topo_mgr/models.py` 的 RELATIONS 一一对应。后端加一种关系时
 * 这里漏配不会崩：走 FALLBACK_STYLE（灰色虚线），但线型就区分不出来了——所以加关系
 * 的同时补这张表。
 */
import { ref } from 'vue'
import { MarkerType } from '@vue-flow/core'

/**
 * 颜色与线型**同时**编码关系，不只靠颜色：约 8% 的男性有红绿色觉障碍，
 * 只用颜色区分等于对他们没做区分。四种实线之间靠色相拉开，虚线族再叠一层线型。
 *
 * dash 为 null = 实线。数组直接喂给 stroke-dasharray。
 */
export const RELATION_STYLES = {
  // DNS 层：主链路，给最醒目的蓝
  resolve: { color: '#409eff', dash: null },
  // 流量转发族
  backend: { color: '#67c23a', dash: null },
  origin: { color: '#9254de', dash: null },
  landing: { color: '#13c2c2', dash: null },
  // 回源是「绕出去再回来」，虚线表达这层间接性
  upstream: { color: '#e6a23c', dash: [6, 4] },
  // 挂载是成员关系不是流量，点线与转发族拉开
  member: { color: '#eb2f96', dash: [2, 4] },
  // 兜底：说不清是什么关系的线，灰色虚线，不抢视线
  custom: { color: '#909399', dash: [4, 4] },
}

//: 后端加了关系而这里没跟上时用它。刻意不用黑色——黑线看起来像「重要」，
//: 而这条线的真实状态是「没登记过」
export const FALLBACK_STYLE = { color: '#c0c4cc', dash: [4, 4] }

/**
 * 粗细。dim 不减粗细只减透明度：变细会让虚线在低透明度下直接消失。
 *
 * dot 是流动圆点的直径（圆点由零长度 dash + 圆头描边画出，见 FLOW_DASH，
 * 所以它的「粗细」就是直径）。7 相对 base 1.8 够显眼，又不至于盖住连线两端的箭头。
 */
export const EDGE_WIDTH = {
  base: 1.8,
  highlight: 3,
  dot: 7,
}

/**
 * 透明度。0.15 是「还看得出有根线，但读不出它连哪儿」——正是 dim 想要的。
 *
 * track 是**跑着流动动画时底下那条线**的 stroke-opacity。这一档是流动看不看得出来的
 * 关键：底线和流动段同色同深，叠在一起只是变粗，没有任何「有东西在走」的感觉。
 * 把底线压淡成一条轨道，圆点跑在上面才有对比。
 *
 * 0.5 而不是更淡：现在每条线上只有一个圆点（见 FLOW_DASH），点不在的那大半截时间里，
 * 这条线就只剩轨道——压太淡等于大部分时候看不清线连去哪儿。
 *
 * 注意用的是 **stroke-opacity 而不是 opacity**：opacity 是元素级的，会把箭头一起压淡，
 * 于是线亮箭头淡；stroke-opacity 只作用于描边，marker 自带 fill 不受影响。
 */
export const EDGE_OPACITY = {
  base: 0.9,
  dim: 0.15,
  highlight: 1,
  track: 0.5,
}

/**
 * 多条线汇聚到同一个连接点时，末端按这个步长在垂直方向扇开。
 *
 * 9px 是「箭头之间能看出缝、又不会脱离卡片边」的量：节点卡片高 78，
 * 三条线扇开占 18px，仍落在卡片边上。
 */
export const FAN_STEP = 9
//: 汇聚超过这个数就不再继续扇，否则末端会甩出卡片外。超出的线挤在最外两档上，
//: 这时候该做的是理图，不是靠画线技巧救
export const FAN_MAX_SPREAD = 36

/**
 * 贝塞尔曲率。0.25 是 vue-flow 的默认值，这里显式写出来只为「它是可调的」这件事
 * 有个地方可改——曲率太大时相邻两条线会鼓成两个并排的气泡，反而更难分。
 */
export const EDGE_CURVATURE = 0.25

/*
  流动的形式。走过四版，记下来免得再走回去：

  1. `6 8` / 0.9s：一个周期 14px ≈ 15px/s，比人眼判定「这在动」的阈值还慢；且亮段与底线
     同色同宽，叠一起只是变粗，没有对比。
  2. `10 16` / 0.65s + 底线压淡：动是看出来了，但**整条线铺满重复亮段**，几十条线同时爬，
     观感是密密麻麻的毛毛虫。
  3. 单段长光（占全长 14%）：不密集了，但一截长条滑过去仍像一条虫在蠕动。
  4. 现在：**一个圆点**沿线走。最安静的一版——点没有长度，也就没有「蠕动」这回事，
     方向由它往哪儿走交代得一清二楚。

  圆点不是另画一个 <circle> 再沿路径跑（那要 animateMotion，每条线多一个 SMIL 节点和
  一次路径采样），仍旧是同一个 dashoffset 动画：dash 段长取到近乎为零，配 `stroke-linecap:
  round`，圆头把这个零长度段渲染成一个圆——直径就是描边宽度（EDGE_WIDTH.dot）。

  取 0.1 而不是 0：零长度子路径在部分渲染器上会被整段跳过，什么都不画。0.1 是归一化单位，
  在最长的连线上也不到半个像素，圆头一盖就看不出它其实是一小截线。
*/
//: 归一化后的全长。取 100 是为了 dash 直接读作百分比
export const FLOW_PATH_LENGTH = 100
const FLOW_DOT_LENGTH = 0.1
export const FLOW_DASH = `${FLOW_DOT_LENGTH} ${FLOW_PATH_LENGTH - FLOW_DOT_LENGTH}`
//: 一轮位移量 = 归一化全长，接得上才不会每轮末尾跳一下
export const FLOW_PERIOD = FLOW_PATH_LENGTH
/*
  跑完一条线的时间。

  这里用 pathLength 归一化的真正代价在这一行：每条线耗时相同，于是**线越长点走得越快**。
  换成按像素定速可以让全图点速一致，但那样短边上的点一闪就没、长边上半天爬不到头，
  反而更难跟。1.6s 是「从容滑过去」的量级。
*/
export const FLOW_DURATION = '1.6s'

/*
  前庭功能敏感的人看满屏爬动的线会不适。原来靠 CSS 的 prefers-reduced-motion 把动画关掉，
  但现在「跑动画」还连带把底线压成淡色轨道（EDGE_OPACITY.track）——只关动画会留下一条
  淡轨道加一个停在起点的圆点，比动着还难读。所以改在 JS 里判定：直接不渲染那条流动路径，
  底线保持原样，高亮就是纯粹的加粗加亮。

  用 ref + change 监听而不是每条边各自 matchMedia：这是全局偏好，一份就够，
  且用户在系统里改了设置能立刻生效。
*/
const motionQuery = typeof window !== 'undefined' && window.matchMedia
  ? window.matchMedia('(prefers-reduced-motion: reduce)')
  : null
export const prefersReducedMotion = ref(motionQuery ? motionQuery.matches : false)
motionQuery?.addEventListener('change', (e) => { prefersReducedMotion.value = e.matches })

/**
 * 流动动画默认全图开着：这张图是用来回答「流量往哪儿走」的，进图第一眼就该看见方向，
 * 而不是先点一条线才肯告诉你。
 *
 * 但它有真实代价：stroke-dashoffset 动不了合成器（不是 transform/opacity），每帧要
 * 重新 paint 一遍那条路径。边一多就是整屏持续重绘。所以给一个自动降级的阈值——
 * 超过就退回「只有选中/高亮的线在动」。
 *
 * 160 的来历：一屏能看清的连线本来也就这个量级，再多的图人已经在靠悬停逐条读了，
 * 全图动画那时候只剩噪声。
 */
export const FLOW_MAX_EDGES = 160

/** 取关系的视觉配置，未知关系回落到 FALLBACK_STYLE。 */
export function relationStyle(relation) {
  return RELATION_STYLES[relation] || FALLBACK_STYLE
}

/** stroke-dasharray 的字符串形式（实线给 undefined，别输出 'none' 字符串）。 */
export function relationDash(relation) {
  const { dash } = relationStyle(relation)
  return dash ? dash.join(' ') : undefined
}

/**
 * 箭头配置。vue-flow 拿对象形式的 markerEnd 会按内容生成一份 `<marker>` 定义并去重
 * （见 core 的 MarkerDefinitions），所以每种关系只会多出一个 defs，与边数无关。
 * 给字符串形式（MarkerType.ArrowClosed）则所有边共用一个默认色箭头——那正是
 * 「线有颜色、箭头统一灰」的来源。
 */
export function relationMarker(relation) {
  return {
    type: MarkerType.ArrowClosed,
    color: relationStyle(relation).color,
    width: 16,
    height: 16,
  }
}

/*
  中段箭头。

  端点那个 marker 一直都在，但它挨着节点卡片，几条线挤在同一个连接点时箭头叠成一小撮，
  要判断某条线往哪儿流得先把视线顺着曲线爬到头。默认不再有流动动画之后（见画布里
  showFlow 的注释），方向就全靠箭头交代，只在端点给一个不够。

  实现方式是在曲线正中插一个顶点，用 CSS 的 marker-mid 挂箭头——浏览器按 orient="auto"
  自动算切线方向，不用自己求导。marker 定义由画布统一渲染一份（每种关系一个颜色），
  这里只负责给出 id。

  markerUnits 用 userSpaceOnUse：默认的 strokeWidth 单位会让箭头跟着线粗变大，
  高亮时线从 1.8 变 3，箭头跟着跳一下，像是图动了。
*/
export const MID_ARROW_SIZE = 9

//: 每种关系一个箭头定义（颜色不同），加一个兜底。画布据此渲染 <defs>
export const ARROW_DEFS = [
  ...Object.entries(RELATION_STYLES).map(([relation, { color }]) => ({
    id: `topo-arrow-${relation}`,
    color,
  })),
  { id: 'topo-arrow-fallback', color: FALLBACK_STYLE.color },
]

/** 中段箭头该引用哪个 marker 定义。未知关系走兜底，和线的颜色保持一致。 */
export function midArrowId(relation) {
  return RELATION_STYLES[relation] ? `topo-arrow-${relation}` : 'topo-arrow-fallback'
}

/**
 * 末端扇形偏移量：同一个「目标节点 + 目标连接点」上的多条线各自让开一点，
 * 否则所有箭头精确重合在一个点上，图上只看得见最后画的那一条。
 *
 * 返回 Map<edgeId, offset>，offset 是标量；朝哪个轴偏由连线自己按
 * targetPosition 决定（左右进 = 偏 Y，上下进 = 偏 X）。
 *
 * 排序用 source id 而不是数组下标：下标会随「谁先被加进来」变，
 * 同一张图两次打开线的上下顺序就不一样，人会以为图改了。
 */
export function computeFanOffsets(edges) {
  const groups = new Map()
  for (const edge of edges) {
    const key = `${edge.target}::${edge.targetHandle || ''}`
    if (!groups.has(key)) groups.set(key, [])
    groups.get(key).push(edge)
  }
  const offsets = new Map()
  for (const group of groups.values()) {
    if (group.length < 2) continue
    const sorted = [...group].sort((a, b) => String(a.source).localeCompare(String(b.source)))
    const step = Math.min(FAN_STEP, FAN_MAX_SPREAD / (sorted.length - 1))
    const mid = (sorted.length - 1) / 2
    sorted.forEach((edge, i) => offsets.set(edge.id, (i - mid) * step))
  }
  return offsets
}
