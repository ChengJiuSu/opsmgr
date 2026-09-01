<!--
  通用右键菜单：给一组菜单项 + 一个屏幕坐标，弹在那儿。

  为什么不用 el-dropdown：它的定位锚在触发元素上，右键菜单要锚在**鼠标位置**，
  硬掰过去要手搓一个虚拟触发元素，比直接写这几十行更绕。

  定位用 fixed（相对视口），刻意不挂在画布内部：画布有 transform 缩放，挂里面
  的话 0.5 倍缩放下菜单字只有一半大。

  菜单项形状：
    { key, label, icon?, disabled?, hint?, checked?, danger?, divider?, children? }
  - divider: true 只画一条分隔线，其余字段忽略
  - children: 二级菜单（悬停展开），有 children 时点父项不触发 select
  - disabled + hint: 置灰并说明为什么不能点（比直接不显示好——用户会找那一项）
-->
<template>
  <Teleport to="body">
    <div
      v-if="visible"
      ref="menuRef"
      class="ctx-menu"
      :style="{ left: `${pos.x}px`, top: `${pos.y}px` }"
      @contextmenu.prevent
    >
      <template v-for="(item, index) in items" :key="item.key || `d${index}`">
        <div v-if="item.divider" class="ctx-divider" />
        <div
          v-else
          class="ctx-item"
          :class="{ disabled: item.disabled, danger: item.danger, parent: item.children }"
          @mouseenter="openChild(item, $event)"
          @click="pick(item)"
        >
          <span class="ctx-label">{{ item.label }}</span>
          <span v-if="item.checked" class="ctx-check">✓</span>
          <span v-else-if="item.children" class="ctx-arrow">›</span>
          <span v-else-if="item.disabled && item.hint" class="ctx-hint">{{ item.hint }}</span>
        </div>
      </template>

      <!-- 二级菜单：同一层 fixed 定位，不做嵌套 DOM，免得被父级 overflow 裁掉 -->
      <div
        v-if="childItems.length"
        class="ctx-menu ctx-submenu"
        :style="{ left: `${childPos.x}px`, top: `${childPos.y}px` }"
      >
        <div
          v-for="child in childItems"
          :key="child.key"
          class="ctx-item"
          :class="{ disabled: child.disabled }"
          @click="pick(child)"
        >
          <span class="ctx-label">{{ child.label }}</span>
          <span v-if="child.checked" class="ctx-check">✓</span>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup>
import { ref, reactive, nextTick, onMounted, onUnmounted } from 'vue'

const props = defineProps({
  items: { type: Array, default: () => [] },
})
const emit = defineEmits(['select', 'close'])

const visible = ref(false)
const menuRef = ref(null)
const pos = reactive({ x: 0, y: 0 })
const childItems = ref([])
const childPos = reactive({ x: 0, y: 0 })

// 估算尺寸用于贴边翻转：等菜单渲染完再量实际尺寸做二次修正
const MENU_WIDTH = 190
const ITEM_HEIGHT = 30

async function open(event) {
  childItems.value = []
  pos.x = event.clientX
  pos.y = event.clientY
  visible.value = true
  await nextTick()
  // 贴边翻转：菜单弹在屏幕右下角时会被裁掉半截，往回折
  const el = menuRef.value
  if (!el) return
  const rect = el.getBoundingClientRect()
  if (rect.right > window.innerWidth - 4) {
    pos.x = Math.max(4, window.innerWidth - rect.width - 4)
  }
  if (rect.bottom > window.innerHeight - 4) {
    pos.y = Math.max(4, window.innerHeight - rect.height - 4)
  }
}

function close() {
  if (!visible.value) return
  visible.value = false
  childItems.value = []
  emit('close')
}

function openChild(item, event) {
  if (!item.children) {
    childItems.value = []
    return
  }
  childItems.value = item.children
  const rect = event.currentTarget.getBoundingClientRect()
  childPos.x = rect.right + 2
  childPos.y = rect.top
  nextTick(() => {
    const height = item.children.length * ITEM_HEIGHT
    if (childPos.x + MENU_WIDTH > window.innerWidth - 4) {
      childPos.x = Math.max(4, rect.left - MENU_WIDTH - 2)
    }
    if (childPos.y + height > window.innerHeight - 4) {
      childPos.y = Math.max(4, window.innerHeight - height - 4)
    }
  })
}

function pick(item) {
  if (item.disabled || item.children) return
  close()
  emit('select', item)
}

// 任何点击、滚轮、Esc 都关掉：右键菜单赖在屏幕上不走是最招人烦的一种 bug
function onGlobalPointerDown(event) {
  if (!visible.value) return
  if (menuRef.value?.contains(event.target)) return
  close()
}

function onKeydown(event) {
  if (event.key === 'Escape') close()
}

onMounted(() => {
  window.addEventListener('pointerdown', onGlobalPointerDown, true)
  window.addEventListener('wheel', close, true)
  window.addEventListener('blur', close)
  window.addEventListener('keydown', onKeydown)
})
onUnmounted(() => {
  window.removeEventListener('pointerdown', onGlobalPointerDown, true)
  window.removeEventListener('wheel', close, true)
  window.removeEventListener('blur', close)
  window.removeEventListener('keydown', onKeydown)
})

defineExpose({ open, close, visible })
</script>

<style scoped>
.ctx-menu {
  position: fixed;
  z-index: 3000;
  min-width: 170px;
  padding: 4px 0;
  background: #fff;
  border: 1px solid #e4e7ed;
  border-radius: 4px;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.12);
  font-size: 13px;
  user-select: none;
}
.ctx-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  color: #303133;
  cursor: pointer;
  line-height: 18px;
}
.ctx-item:hover { background: #f5f7fa; }
.ctx-item.danger { color: #f56c6c; }
.ctx-item.disabled { color: #c0c4cc; cursor: not-allowed; background: none; }
.ctx-label { flex: 1; white-space: nowrap; }
.ctx-hint { font-size: 11px; color: #c0c4cc; }
.ctx-check { color: #409eff; }
.ctx-arrow { color: #909399; }
.ctx-divider { height: 1px; margin: 4px 0; background: #f0f2f5; }
</style>
