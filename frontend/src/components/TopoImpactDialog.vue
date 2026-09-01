<!--
  影响面反查弹窗：这台主机 / 这个域名在哪些拓扑画布上，上下游是谁。

  做成通用组件而不是各页面各写一份：服务器页、域名页问的是同一个问题，两份实现
  迟早在「上游/下游谁是谁」上说反。用 v-model 控制显隐，父页面只需给 kind + refId。

  权限：调用方自己判 `authStore.can('topo')` 再决定要不要挂入口——没有画布权限的人
  看到一个点开就 403 的按钮只会困惑。
-->
<template>
  <el-dialog
    :model-value="modelValue"
    :title="`所在拓扑 · ${label || ''}`"
    width="720px"
    @update:model-value="$emit('update:modelValue', $event)"
    @open="fetchData"
  >
    <div v-loading="loading">
      <el-empty
        v-if="!loading && !results.length"
        :description="emptyText"
        :image-size="80"
      />
      <div v-for="hit in results" :key="hit.canvas.id + '-' + hit.node.id" class="hit">
        <div class="hit-head">
          <el-link type="primary" @click="openCanvas(hit.canvas)">
            {{ hit.canvas.name }}
          </el-link>
          <el-tag size="small" type="info">{{ hit.canvas.project_group_name }}</el-tag>
          <el-tag size="small" type="info">{{ hit.canvas.channel_name }}</el-tag>
          <el-tag size="small" :type="hit.canvas.is_prod ? 'danger' : 'success'">
            {{ hit.canvas.environment_name }}
          </el-tag>
          <span class="muted">{{ hit.canvas.region || '跨地域' }}</span>
        </div>
        <div class="hit-body">
          <div class="side">
            <div class="side-title">上游（谁的流量落到它身上）</div>
            <div v-if="!hit.upstream.length" class="muted">—</div>
            <div v-for="(item, i) in hit.upstream" :key="'u' + i" class="neighbour">
              <el-tag size="small" effect="plain">{{ item.relation_label }}</el-tag>
              <span :class="['n-label', `health-${item.node.health}`]">
                {{ item.node.label }}
              </span>
              <span class="muted">{{ item.node.kind_label }}</span>
            </div>
          </div>
          <div class="side">
            <div class="side-title">下游（它指向谁）</div>
            <div v-if="!hit.downstream.length" class="muted">—</div>
            <div v-for="(item, i) in hit.downstream" :key="'d' + i" class="neighbour">
              <el-tag size="small" effect="plain">{{ item.relation_label }}</el-tag>
              <span :class="['n-label', `health-${item.node.health}`]">
                {{ item.node.label }}
              </span>
              <span class="muted">{{ item.node.kind_label }}</span>
            </div>
          </div>
        </div>
      </div>
    </div>
    <template #footer>
      <span v-if="results.length" class="muted footer-hint">
        共 {{ results.length }} 处。下线或改动前先确认上游是否还有流量指过来。
      </span>
      <el-button @click="$emit('update:modelValue', false)">关闭</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { ref, computed } from 'vue'
import { useRouter } from 'vue-router'
import { getTopoImpact } from '../api/topo'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  // kind = server / domain / dns_record / certificate
  kind: { type: String, required: true },
  refId: { type: [Number, String], default: null },
  // 标题上显示的名字；不传则用后端返回的实时名称
  label: { type: String, default: '' },
})
defineEmits(['update:modelValue'])

const router = useRouter()
const loading = ref(false)
const results = ref([])
const entityMissing = ref(false)

const emptyText = computed(() => (entityMissing.value
  ? '该资源已不存在'
  : '这个资源还没有被画进任何拓扑画布'))

async function fetchData() {
  if (!props.refId) return
  loading.value = true
  results.value = []
  try {
    const res = await getTopoImpact({ kind: props.kind, ref_id: props.refId })
    results.value = res.data?.results || []
    entityMissing.value = !res.data?.label
  } finally {
    loading.value = false
  }
}

function openCanvas(canvas) {
  router.push({ path: canvas.link.path, query: canvas.link.query || {} })
}
</script>

<style scoped>
.hit { border: 1px solid #e4e7ed; border-radius: 4px; padding: 10px; margin-bottom: 10px; }
.hit-head { display: flex; align-items: center; gap: 6px; margin-bottom: 8px; }
.hit-body { display: flex; gap: 16px; }
.side { flex: 1; min-width: 0; }
.side-title { font-size: 12px; color: #606266; font-weight: 600; margin-bottom: 4px; }
.neighbour { display: flex; align-items: center; gap: 6px; line-height: 2; font-size: 12px; }
.n-label { color: #303133; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
/* 健康度配色与画布节点、列表页 el-tag 同口径 */
.n-label.health-warning { color: #e6a23c; }
.n-label.health-danger { color: #f56c6c; }
.muted { color: #909399; font-size: 12px; }
.footer-hint { margin-right: 12px; }
</style>
