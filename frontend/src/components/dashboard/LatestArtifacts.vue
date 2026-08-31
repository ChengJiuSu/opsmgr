<template>
  <el-table
    v-if="items.length"
    :data="items"
    size="small"
    max-height="240"
    @row-click="() => router.push('/task/artifacts')"
  >
    <el-table-column prop="environment" label="环境" width="80" />
    <el-table-column prop="project_group" label="项目组" min-width="90" show-overflow-tooltip />
    <el-table-column prop="channel" label="渠道" width="80" show-overflow-tooltip />
    <el-table-column label="当前版本" min-width="110">
      <template #default="{ row }">
        <span class="version">{{ row.version }}</span>
        <span v-if="row.build_number" class="build">#{{ row.build_number }}</span>
      </template>
    </el-table-column>
    <el-table-column label="上报" width="92">
      <template #default="{ row }">{{ time(row.reported_at) }}</template>
    </el-table-column>
  </el-table>
  <el-empty v-else description="还没有制品上报" :image-size="60" />
</template>

<script setup>
import { useRouter } from 'vue-router'

defineProps({
  items: { type: Array, default: () => [] },
})

const router = useRouter()

const time = (t) => (t ? t.slice(5, 16).replace('T', ' ') : '')
</script>

<style scoped>
.version { font-family: monospace; color: #303133; }
.build { margin-left: 6px; color: #c0c4cc; font-size: 12px; }
</style>
