<template>
  <div class="duty">
    <div v-for="block in blocks" :key="block.key" class="block" :class="block.level">
      <div class="block-head" @click="go(block.link)">
        <el-icon class="block-icon"><component :is="icon(block.level)" /></el-icon>
        <span class="block-title">{{ block.title }}</span>
        <el-icon class="block-arrow"><ArrowRight /></el-icon>
      </div>
      <ul v-if="block.items.length" class="items">
        <li
          v-for="item in block.items"
          :key="item.id ?? item.name"
          :class="{ clickable: !!item.link }"
          @click="go(item.link)"
        >
          <span class="item-name">{{ item.name }}</span>
          <!-- 所在链路图：从「什么坏了」一步跳到「它挂在哪条链路上」。
               @click.stop 免得连带触发条目自己的跳转 -->
          <span v-if="item.topo?.length" class="item-topo">
            <el-tag
              v-for="canvas in item.topo"
              :key="canvas.id"
              size="small"
              :type="canvas.is_prod ? 'danger' : 'info'"
              effect="plain"
              class="topo-tag"
              @click.stop="go(canvas.link)"
            >{{ canvas.name }}</el-tag>
          </span>
          <span class="item-meta">{{ meta(block.key, item) }}</span>
        </li>
        <!-- 明细截断到后端的 ITEM_LIMIT，全量数在标题里，这里补一句免得以为只有这些 -->
        <li v-if="block.total > block.items.length" class="more" @click="go(block.link)">
          还有 {{ block.total - block.items.length }} 条，查看全部
        </li>
      </ul>
    </div>
  </div>
</template>

<script setup>
import { CircleCloseFilled, WarningFilled, ArrowRight } from '@element-plus/icons-vue'
import { useRouter } from 'vue-router'

defineProps({
  blocks: { type: Array, default: () => [] },
})

const router = useRouter()

const icon = (level) => (level === 'danger' ? CircleCloseFilled : WarningFilled)

function go(link) {
  if (link?.path) router.push(link)
}

// 相对时间：值班场景只关心「多久了」，绝对时间点要靠鼠标悬停去看
function ago(iso) {
  if (!iso) return '从未'
  return human((Date.now() - new Date(iso).getTime()) / 1000)
}

function human(seconds) {
  const s = Math.max(0, Math.round(seconds))
  if (s < 60) return `${s} 秒`
  if (s < 3600) return `${Math.round(s / 60)} 分钟`
  if (s < 86400) return `${Math.round(s / 3600)} 小时`
  return `${Math.round(s / 86400)} 天`
}

/** 每类块的右侧副信息：同一个组件渲染所有块，差异只在这一行。 */
function meta(key, item) {
  switch (key) {
    case 'cert_expiring':
      return item.days_left < 0
        ? `已过期 ${-item.days_left} 天`
        : `剩 ${item.days_left} 天${item.auto_renew ? '' : '（未开自动续签）'}`
    case 'cert_failed':
      return item.error || ''
    case 'cert_issue_stuck':
    case 'cert_deploy_stuck':
      return `停在中间态 ${ago(item.updated_at)}`
    case 'task_failed':
      return `${item.triggered_by || '-'} · ${ago(item.finished_at)}前`
    case 'agent_offline':
      return `最近心跳 ${ago(item.last_heartbeat)}前`
    case 'task_paused':
      return [
        item.pause_type_label,
        item.waiting_seconds != null ? `已等待 ${human(item.waiting_seconds)}` : '',
      ].filter(Boolean).join(' · ')
    case 'topo_issues':
      // 后端已经把「几个异常 + 头两条原因」压成一句话，前端不再拆
      return item.detail || ''
    case 'dns_approval':
      // 自己提交的单子后端禁止自批，标出来免得申请人一直等自己
      return [
        item.self_submitted ? '你提交的，需他人审批' : `申请人 ${item.requested_by || '-'}`,
        `${human(item.expires_in)}后过期`,
      ].join(' · ')
    default:
      return ''
  }
}
</script>

<style scoped>
.duty { display: flex; flex-direction: column; gap: 10px; }
.item-topo { display: inline-flex; gap: 4px; margin-left: 6px; }
.topo-tag { cursor: pointer; }
.topo-tag:hover { text-decoration: underline; }
.block { border-left: 3px solid #e6a23c; background: #fdf6ec; border-radius: 4px; padding: 10px 12px; }
.block.danger { border-left-color: #f56c6c; background: #fef0f0; }

.block-head { display: flex; align-items: center; gap: 6px; cursor: pointer; }
.block-icon { color: #e6a23c; }
.block.danger .block-icon { color: #f56c6c; }
.block-title { font-weight: 600; color: #303133; }
.block-arrow { color: #c0c4cc; font-size: 12px; }
.block-head:hover .block-arrow { color: #909399; }

.items { list-style: none; margin: 6px 0 0; padding: 0 0 0 22px; }
.items li {
  display: flex; justify-content: space-between; gap: 12px;
  font-size: 13px; color: #606266; line-height: 22px;
}
.items li.clickable { cursor: pointer; }
.items li.clickable:hover .item-name { color: #409eff; }
.item-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.item-meta { flex: none; color: #909399; font-size: 12px; }
.more { color: #909399; cursor: pointer; font-size: 12px; }
.more:hover { color: #409eff; }
</style>
