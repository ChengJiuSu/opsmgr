<template>
  <el-card shadow="never">
    <el-tabs v-model="activeTab" @tab-change="onTabChange">
      <!-- ── 订阅规则 ───────────────────────────────────── -->
      <el-tab-pane label="订阅规则" name="rules">
        <div class="tip block">
          规则决定「哪些事件、哪些项目组 / 渠道 / 环境的通知发给谁」，维度留空表示不限。
          发送方式二选一：<b>应用机器人</b>发到「飞书接入」里登记的会话；<b>群机器人</b>只需群里的
          Webhook，无需配置飞书应用。任务上关闭「消息通知」后，该任务的结果不再发送。
        </div>
        <div class="toolbar">
          <div class="filters">
            <el-input v-model="ruleQuery.keyword" placeholder="搜索规则名称 / 备注" clearable
                      :prefix-icon="Search" style="width:220px" @change="reloadRules" />
            <el-select v-model="ruleQuery.target_type" placeholder="发送方式" clearable
                       style="width:140px" @change="reloadRules">
              <el-option label="应用机器人" value="feishu_chat" />
              <el-option label="群机器人" value="feishu_webhook" />
            </el-select>
          </div>
          <el-button v-if="canWrite" type="primary" :icon="Plus" @click="openRule()">新增规则</el-button>
        </div>

        <el-table v-loading="ruleLoading" :data="rules" stripe>
          <el-table-column prop="name" label="规则名称" min-width="150" />
          <el-table-column label="订阅事件" min-width="200">
            <template #default="{ row }">
              <el-tag v-for="label in row.event_labels" :key="label" size="small"
                      :type="eventTagType(label)" class="gap">{{ label }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="维度" min-width="200">
            <template #default="{ row }">
              <div v-for="dim in dimLines(row)" :key="dim.label" class="dim-line">
                <span class="muted">{{ dim.label }}：</span>
                <span v-if="dim.names.length">{{ dim.names.join('、') }}</span>
                <span v-else class="unset">不限</span>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="发送目标" min-width="170">
            <template #default="{ row }">
              <el-tag size="small" :type="row.target_type === 'feishu_webhook' ? 'warning' : ''">
                {{ row.target_type_display }}
              </el-tag>
              <span class="target-name">{{ row.target_name || '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="@ 发起人" width="90">
            <template #default="{ row }">
              <span v-if="row.mention_trigger">是</span>
              <span v-else class="unset">否</span>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="80">
            <template #default="{ row }">
              <el-switch v-if="canWrite" v-model="row.enabled" size="small" @change="toggleRule(row)" />
              <el-tag v-else :type="row.enabled ? 'success' : 'info'" size="small">
                {{ row.enabled ? '启用' : '停用' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column v-if="canWrite" label="操作" width="220" fixed="right">
            <template #default="{ row }">
              <el-button size="small" :icon="Promotion" @click="handleTestRule(row)">测试</el-button>
              <el-button size="small" :icon="Edit" @click="openRule(row)">编辑</el-button>
              <el-button size="small" type="danger" :icon="Delete" @click="removeRule(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
        <el-pagination
          v-model:current-page="ruleQuery.page"
          v-model:page-size="ruleQuery.page_size"
          :page-sizes="[20, 50, 100]"
          :total="ruleTotal"
          layout="total, sizes, prev, pager, next"
          class="pager"
          @size-change="loadRules"
          @current-change="loadRules"
        />
      </el-tab-pane>

      <!-- ── 群机器人 ───────────────────────────────────── -->
      <el-tab-pane label="群机器人" name="robots">
        <div class="tip block">
          飞书群 → 设置 → 群机器人 → 添加「自定义机器人」，复制 Webhook 地址粘贴到这里。
          机器人的「安全设置」开了签名校验时填上签名密钥；设了自定义关键词时，消息标题里需包含该关键词
          （如「任务」）。群机器人只能发送、不能交互，「重试 / 跳过」仍需在平台或应用机器人卡片上处理。
        </div>
        <div class="toolbar">
          <el-input v-model="robotQuery.keyword" placeholder="搜索机器人名称 / 备注" clearable
                    :prefix-icon="Search" style="width:220px" @change="reloadRobots" />
          <el-button v-if="canWrite" type="primary" :icon="Plus" @click="openRobot()">新增机器人</el-button>
        </div>

        <el-table v-loading="robotLoading" :data="robots" stripe>
          <el-table-column prop="name" label="机器人名称" min-width="150" />
          <el-table-column prop="masked_token" label="Webhook token" min-width="150" />
          <el-table-column label="签名校验" width="100">
            <template #default="{ row }">
              <el-tag :type="row.has_secret ? 'success' : 'info'" size="small">
                {{ row.has_secret ? '已配置' : '未配置' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="订阅规则" width="90">
            <template #default="{ row }">
              <span v-if="row.rule_count">{{ row.rule_count }} 条</span>
              <span v-else class="unset">—</span>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="80">
            <template #default="{ row }">
              <el-tag :type="row.enabled ? 'success' : 'info'" size="small">
                {{ row.enabled ? '启用' : '停用' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="remark" label="备注" show-overflow-tooltip>
            <template #default="{ row }">
              <span v-if="row.remark">{{ row.remark }}</span>
              <span v-else class="unset">—</span>
            </template>
          </el-table-column>
          <el-table-column v-if="canWrite" label="操作" width="220" fixed="right">
            <template #default="{ row }">
              <el-button size="small" :icon="Promotion" @click="handleTestRobot(row)">测试</el-button>
              <el-button size="small" :icon="Edit" @click="openRobot(row)">编辑</el-button>
              <el-button size="small" type="danger" :icon="Delete" @click="removeRobot(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
        <el-pagination
          v-model:current-page="robotQuery.page"
          v-model:page-size="robotQuery.page_size"
          :page-sizes="[20, 50, 100]"
          :total="robotTotal"
          layout="total, sizes, prev, pager, next"
          class="pager"
          @size-change="loadRobots"
          @current-change="loadRobots"
        />
      </el-tab-pane>

      <!-- ── 发送记录 ───────────────────────────────────── -->
      <el-tab-pane label="发送记录" name="deliveries">
        <div class="toolbar">
          <div class="filters">
            <el-input v-model="deliveryQuery.keyword" placeholder="搜索标题 / 规则 / 目标" clearable
                      :prefix-icon="Search" style="width:220px" @change="reloadDeliveries" />
            <el-select v-model="deliveryQuery.event" placeholder="事件" clearable
                       style="width:200px" @change="reloadDeliveries">
              <el-option v-for="e in eventTypes" :key="e.code" :label="e.label" :value="e.code" />
            </el-select>
            <el-select v-model="deliveryQuery.status" placeholder="状态" clearable
                       style="width:120px" @change="reloadDeliveries">
              <el-option v-for="(meta, key) in deliveryStatus" :key="key"
                         :label="meta.label" :value="key" />
            </el-select>
          </div>
          <el-button :icon="Refresh" @click="loadDeliveries">刷新</el-button>
        </div>

        <el-table v-loading="deliveryLoading" :data="deliveries" stripe>
          <el-table-column label="时间" width="170">
            <template #default="{ row }">{{ formatTime(row.sent_at || row.created_at) }}</template>
          </el-table-column>
          <el-table-column prop="event_label" label="事件" min-width="150" />
          <el-table-column prop="title" label="消息" min-width="150" show-overflow-tooltip />
          <el-table-column label="规则" min-width="130">
            <template #default="{ row }">
              <span v-if="row.rule_name">{{ row.rule_name }}</span>
              <span v-else class="unset">—</span>
            </template>
          </el-table-column>
          <el-table-column label="发送目标" min-width="170">
            <template #default="{ row }">
              <el-tag size="small" :type="row.target_type === 'feishu_webhook' ? 'warning' : ''">
                {{ row.target_type_display }}
              </el-tag>
              <span class="target-name">{{ row.target_name || '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <el-tag :type="deliveryStatus[row.status]?.type" size="small">
                {{ row.status_display }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="error" label="说明" min-width="200" show-overflow-tooltip>
            <template #default="{ row }">
              <span v-if="row.error" :class="{ danger: row.status === 'failed' }">{{ row.error }}</span>
              <span v-else class="unset">—</span>
            </template>
          </el-table-column>
        </el-table>
        <el-pagination
          v-model:current-page="deliveryQuery.page"
          v-model:page-size="deliveryQuery.page_size"
          :page-sizes="[20, 50, 100]"
          :total="deliveryTotal"
          layout="total, sizes, prev, pager, next"
          class="pager"
          @size-change="loadDeliveries"
          @current-change="loadDeliveries"
        />
      </el-tab-pane>
    </el-tabs>
  </el-card>

  <!-- 规则弹窗 -->
  <el-dialog v-model="ruleVisible" :title="editRule ? '编辑订阅规则' : '新增订阅规则'" width="600px">
    <el-form ref="ruleFormRef" :model="ruleForm" :rules="ruleRules" label-width="110px">
      <el-form-item label="规则名称" prop="name">
        <el-input v-model="ruleForm.name" maxlength="100" placeholder="如：主项目生产失败告警" />
      </el-form-item>
      <el-form-item label="订阅事件" prop="events">
        <div v-for="group in eventGroups" :key="group.name" class="event-group">
          <div class="muted">{{ group.name }}</div>
          <el-checkbox-group v-model="ruleForm.events">
            <el-checkbox v-for="e in group.items" :key="e.code" :value="e.code">{{ e.label }}</el-checkbox>
          </el-checkbox-group>
        </div>
      </el-form-item>
      <el-form-item label="发送方式" prop="target_type">
        <el-radio-group v-model="ruleForm.target_type">
          <el-radio value="feishu_chat">应用机器人</el-radio>
          <el-radio value="feishu_webhook">群机器人</el-radio>
        </el-radio-group>
      </el-form-item>
      <el-form-item v-if="ruleForm.target_type === 'feishu_chat'" label="发送到会话" prop="feishu_chat">
        <el-select v-model="ruleForm.feishu_chat" filterable placeholder="选择已登记的飞书会话"
                   style="width:100%">
          <el-option v-for="c in chatOptions" :key="c.id" :value="c.id"
                     :label="`${c.name}（${c.chat_type_display}）`" :disabled="!c.enabled" />
        </el-select>
        <div class="tip">会话在「系统管理 → 飞书接入」里登记，需要飞书应用已启用。</div>
      </el-form-item>
      <el-form-item v-else label="群机器人" prop="webhook_robot">
        <el-select v-model="ruleForm.webhook_robot" filterable placeholder="选择群机器人"
                   style="width:100%">
          <el-option v-for="r in robotOptions" :key="r.id" :value="r.id" :label="r.name"
                     :disabled="!r.enabled" />
        </el-select>
      </el-form-item>
      <el-form-item label="项目组">
        <el-select v-model="ruleForm.project_groups" multiple clearable filterable
                   placeholder="不限" style="width:100%">
          <el-option v-for="d in dictOptions.projects" :key="d.id" :label="d.name" :value="d.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="渠道">
        <el-select v-model="ruleForm.channels" multiple clearable filterable
                   placeholder="不限" style="width:100%">
          <el-option v-for="d in dictOptions.channels" :key="d.id" :label="d.name" :value="d.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="环境">
        <el-select v-model="ruleForm.environments" multiple clearable filterable
                   placeholder="不限" style="width:100%">
          <el-option v-for="d in dictOptions.envs" :key="d.id" :label="d.name" :value="d.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="@ 发起人">
        <el-switch v-model="ruleForm.mention_trigger" />
        <span class="tip inline">发起人需已绑定飞书账号</span>
      </el-form-item>
      <el-form-item label="启用">
        <el-switch v-model="ruleForm.enabled" />
      </el-form-item>
      <el-form-item label="备注">
        <el-input v-model="ruleForm.remark" maxlength="200" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="ruleVisible = false">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="saveRule">保存</el-button>
    </template>
  </el-dialog>

  <!-- 群机器人弹窗 -->
  <el-dialog v-model="robotVisible" :title="editRobot ? '编辑群机器人' : '新增群机器人'" width="560px">
    <el-form ref="robotFormRef" :model="robotForm" :rules="robotRules" label-width="110px">
      <el-form-item label="机器人名称" prop="name">
        <el-input v-model="robotForm.name" maxlength="100" placeholder="如：运维告警群机器人" />
      </el-form-item>
      <el-form-item label="Webhook" prop="hook_token">
        <el-input v-model="robotForm.hook_token" type="password" show-password
                  :placeholder="editRobot
                    ? `已配置（${editRobot.masked_token}），留空不修改`
                    : 'https://open.feishu.cn/open-apis/bot/v2/hook/xxxx 或其中的 token'" />
      </el-form-item>
      <el-form-item label="签名密钥">
        <el-input v-model="robotForm.secret" type="password" show-password
                  :disabled="robotForm.clear_secret"
                  :placeholder="editRobot?.has_secret ? '已配置，留空不修改' : '未开启签名校验可不填'" />
        <el-checkbox v-if="editRobot?.has_secret" v-model="robotForm.clear_secret">
          清除签名密钥（机器人已关闭签名校验时勾选）
        </el-checkbox>
      </el-form-item>
      <el-form-item label="启用">
        <el-switch v-model="robotForm.enabled" />
      </el-form-item>
      <el-form-item label="备注">
        <el-input v-model="robotForm.remark" maxlength="200" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="robotVisible = false">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="saveRobot">保存</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Delete, Edit, Plus, Promotion, Refresh, Search } from '@element-plus/icons-vue'
import {
  createRobot, createRule, deleteRobot, deleteRule, getDeliveries, getEventTypes,
  getChatOptions, getRobots, getRules, patchRule, testRobot, testRule, updateRobot, updateRule,
} from '../../api/notify'
import { channelApi, environmentApi, fetchDictOptions, projectGroupApi } from '../../api/assets'
import { useAuthStore } from '../../stores/auth'

const authStore = useAuthStore()
// 只读档（notify=read）只能看规则、机器人与发送记录，增删改与测试发送都要 write
const canWrite = computed(() => authStore.can('notify', 'write'))
const activeTab = ref('rules')
const submitting = ref(false)

const deliveryStatus = {
  success: { label: '已发送', type: 'success' },
  failed: { label: '发送失败', type: 'danger' },
  skipped: { label: '已跳过', type: 'info' },
  pending: { label: '发送中', type: 'warning' },
}

function formatTime(value) {
  return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—'
}

// ── 公共选项 ──
const eventTypes = ref([])
const eventGroups = computed(() => {
  const groups = []
  for (const e of eventTypes.value) {
    let group = groups.find((g) => g.name === e.group)
    if (!group) groups.push(group = { name: e.group, items: [] })
    group.items.push(e)
  }
  return groups
})
const dictOptions = reactive({ projects: [], channels: [], envs: [] })
const chatOptions = ref([])
const robotOptions = ref([])

function eventTagType(label) {
  if (label.includes('成功')) return 'success'
  if (label.includes('失败')) return 'danger'
  return 'info'
}

async function loadOptions() {
  const types = await getEventTypes()
  eventTypes.value = types.data
  // 维度下拉要 dict 档：没给就不取，规则仍可保存（维度留空 = 不限），
  // 不能因为一个 403 把整页选项都带走
  if (!authStore.can('dict')) return
  const [projects, channels, envs] = await Promise.all([
    fetchDictOptions(projectGroupApi),
    fetchDictOptions(channelApi),
    fetchDictOptions(environmentApi),
  ])
  dictOptions.projects = projects.data.results
  dictOptions.channels = channels.data.results
  dictOptions.envs = envs.data.results
}

async function loadTargetOptions() {
  const [chats, robots] = await Promise.all([
    getChatOptions(),
    getRobots({ page_size: 200 }),
  ])
  chatOptions.value = chats.data
  robotOptions.value = robots.data.results
}

// ── 订阅规则 ──
const rules = ref([])
const ruleTotal = ref(0)
const ruleLoading = ref(false)
const ruleQuery = reactive({ keyword: '', target_type: '', page: 1, page_size: 20 })
const ruleVisible = ref(false)
const editRule = ref(null)
const ruleFormRef = ref()
const emptyRule = () => ({
  name: '', enabled: true,
  events: eventTypes.value.filter((e) => e.default).map((e) => e.code),
  target_type: 'feishu_webhook', feishu_chat: null, webhook_robot: null,
  project_groups: [], channels: [], environments: [],
  mention_trigger: false, remark: '',
})
const ruleForm = reactive(emptyRule())
const ruleRules = {
  name: [{ required: true, message: '请输入规则名称', trigger: 'blur' }],
  events: [{ type: 'array', required: true, min: 1, message: '至少选择一种事件', trigger: 'change' }],
  feishu_chat: [{ required: true, message: '请选择会话', trigger: 'change' }],
  webhook_robot: [{ required: true, message: '请选择群机器人', trigger: 'change' }],
}

function dimLines(row) {
  return [
    { label: '项目组', names: row.project_group_names },
    { label: '渠道', names: row.channel_names },
    { label: '环境', names: row.environment_names },
  ]
}

async function loadRules() {
  ruleLoading.value = true
  try {
    const res = await getRules({
      keyword: ruleQuery.keyword || undefined,
      target_type: ruleQuery.target_type || undefined,
      page: ruleQuery.page,
      page_size: ruleQuery.page_size,
    })
    rules.value = res.data.results
    ruleTotal.value = res.data.total
  } finally {
    ruleLoading.value = false
  }
}

function reloadRules() {
  ruleQuery.page = 1
  loadRules()
}

async function openRule(row = null) {
  editRule.value = row
  Object.assign(ruleForm, row ? {
    name: row.name, enabled: row.enabled, events: [...row.events],
    target_type: row.target_type, feishu_chat: row.feishu_chat, webhook_robot: row.webhook_robot,
    project_groups: [...row.project_groups], channels: [...row.channels],
    environments: [...row.environments],
    mention_trigger: row.mention_trigger, remark: row.remark || '',
  } : emptyRule())
  ruleVisible.value = true
  await loadTargetOptions()
  ruleFormRef.value?.clearValidate()
}

async function saveRule() {
  await ruleFormRef.value.validate()
  submitting.value = true
  try {
    if (editRule.value) {
      await updateRule(editRule.value.id, { ...ruleForm })
    } else {
      await createRule({ ...ruleForm })
    }
    ElMessage.success('已保存')
    ruleVisible.value = false
    loadRules()
  } finally {
    submitting.value = false
  }
}

async function toggleRule(row) {
  try {
    await patchRule(row.id, { enabled: row.enabled })
    ElMessage.success(row.enabled ? '已启用' : '已停用')
  } catch {
    row.enabled = !row.enabled
  }
}

async function handleTestRule(row) {
  const res = await testRule(row.id)
  ElMessage.success(res.msg || '测试消息已发送')
}

async function removeRule(row) {
  await ElMessageBox.confirm(
    `删除后「${row.target_name}」将不再收到该规则订阅的通知。确认删除规则「${row.name}」吗？`,
    '删除规则', { type: 'warning' },
  )
  await deleteRule(row.id)
  ElMessage.success('已删除')
  loadRules()
}

// ── 群机器人 ──
const robots = ref([])
const robotTotal = ref(0)
const robotLoading = ref(false)
const robotQuery = reactive({ keyword: '', page: 1, page_size: 20 })
const robotVisible = ref(false)
const editRobot = ref(null)
const robotFormRef = ref()
const robotForm = reactive({
  name: '', hook_token: '', secret: '', clear_secret: false, enabled: true, remark: '',
})
const robotRules = {
  name: [{ required: true, message: '请输入机器人名称', trigger: 'blur' }],
  hook_token: [{
    validator: (rule, value, callback) => {
      if (!editRobot.value && !value) callback(new Error('请填写 Webhook 地址或 token'))
      else callback()
    },
    trigger: 'blur',
  }],
}

async function loadRobots() {
  robotLoading.value = true
  try {
    const res = await getRobots({
      keyword: robotQuery.keyword || undefined,
      page: robotQuery.page,
      page_size: robotQuery.page_size,
    })
    robots.value = res.data.results
    robotTotal.value = res.data.total
  } finally {
    robotLoading.value = false
  }
}

function reloadRobots() {
  robotQuery.page = 1
  loadRobots()
}

function openRobot(row = null) {
  editRobot.value = row
  // 秘密字段一律回填空串：后端把留空视为「不修改」
  Object.assign(robotForm, {
    name: row?.name || '', hook_token: '', secret: '', clear_secret: false,
    enabled: row ? row.enabled : true, remark: row?.remark || '',
  })
  robotVisible.value = true
  robotFormRef.value?.clearValidate()
}

async function saveRobot() {
  await robotFormRef.value.validate()
  submitting.value = true
  try {
    if (editRobot.value) {
      await updateRobot(editRobot.value.id, { ...robotForm })
    } else {
      await createRobot({ ...robotForm })
    }
    ElMessage.success('已保存')
    robotVisible.value = false
    loadRobots()
  } finally {
    submitting.value = false
  }
}

async function handleTestRobot(row) {
  const res = await testRobot(row.id)
  ElMessage.success(res.msg || '测试消息已发送')
}

async function removeRobot(row) {
  const extra = row.rule_count ? `发往它的 ${row.rule_count} 条订阅规则会一并删除，` : ''
  await ElMessageBox.confirm(
    `${extra}删除后该群将收不到平台通知。确认删除群机器人「${row.name}」吗？`,
    '删除群机器人', { type: 'warning' },
  )
  await deleteRobot(row.id)
  ElMessage.success('已删除')
  loadRobots()
}

// ── 发送记录 ──
const deliveries = ref([])
const deliveryTotal = ref(0)
const deliveryLoading = ref(false)
const deliveryQuery = reactive({ keyword: '', event: '', status: '', page: 1, page_size: 20 })

async function loadDeliveries() {
  deliveryLoading.value = true
  try {
    const res = await getDeliveries({
      keyword: deliveryQuery.keyword || undefined,
      event: deliveryQuery.event || undefined,
      status: deliveryQuery.status || undefined,
      page: deliveryQuery.page,
      page_size: deliveryQuery.page_size,
    })
    deliveries.value = res.data.results
    deliveryTotal.value = res.data.total
  } finally {
    deliveryLoading.value = false
  }
}

function reloadDeliveries() {
  deliveryQuery.page = 1
  loadDeliveries()
}

function onTabChange(name) {
  if (name === 'robots') loadRobots()
  else if (name === 'deliveries') loadDeliveries()
  else loadRules()
}

onMounted(async () => {
  await loadOptions()
  loadRules()
})
</script>

<style scoped>
.toolbar {
  display: flex;
  justify-content: space-between;
  margin-bottom: 12px;
}
.filters {
  display: flex;
  gap: 8px;
}
.pager {
  margin-top: 12px;
  justify-content: flex-end;
}
.tip {
  font-size: 12px;
  color: #909399;
  line-height: 1.6;
}
.tip.block {
  margin-bottom: 12px;
}
.tip.inline {
  margin-left: 10px;
}
.gap {
  margin: 2px 4px 2px 0;
}
.dim-line {
  font-size: 12px;
  line-height: 1.7;
}
.target-name {
  margin-left: 6px;
}
.event-group + .event-group {
  margin-top: 4px;
}
.muted {
  color: #909399;
}
.unset {
  color: #C0C4CC;
}
.danger {
  color: #F56C6C;
}
</style>
