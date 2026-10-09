<template>
  <el-alert
    v-if="app.missing_fields?.length"
    type="warning"
    :closable="false"
    show-icon
    style="margin-bottom:12px"
  >
    启用飞书接入前还需配置：{{ app.missing_fields.join('、') }}
  </el-alert>

  <!-- ── 应用配置 ─────────────────────────────────────── -->
  <el-card v-loading="loading" shadow="never" style="margin-bottom:16px">
    <template #header>
      <div class="card-header">
        <span>飞书应用</span>
        <el-tag :type="app.enabled ? 'success' : 'info'" size="small">
          {{ app.enabled ? '已启用' : '未启用' }}
        </el-tag>
      </div>
    </template>

    <el-form :model="form" label-width="150px" style="max-width:760px">
      <el-form-item label="App ID">
        <el-input v-model="form.app_id" placeholder="cli_xxxxxxxxxxxx" />
      </el-form-item>
      <el-form-item v-for="item in secretFields" :key="item.key" :label="item.label">
        <el-input
          v-model="form[item.key]"
          type="password"
          show-password
          :placeholder="app[item.has] ? '已配置，留空表示不修改' : item.placeholder"
        />
      </el-form-item>

      <el-divider content-position="left">事件接收方式</el-divider>
      <el-form-item label="接收方式">
        <el-radio-group v-model="form.event_mode">
          <el-radio value="callback">回调（飞书推到公网地址）</el-radio>
          <el-radio value="websocket">长连接（平台主动外连）</el-radio>
        </el-radio-group>
        <div class="tip">
          <b>长连接入站零暴露</b>：平台主动外连飞书，下面那两条回调地址不再需要，
          服务可以收回内网、只对指定 IP 开放。代价是多一个常驻进程
          （<code>manage.py feishu_ws</code>，supervisor 配置见
          <code>deploy/opsmgr-feishu-ws.conf</code>），且执行后端必须是 celery。
          <br />
          两种方式<b>互斥</b>：切到长连接后请把飞书后台的「请求地址」清掉，
          否则同一个事件会被处理两次。
        </div>
      </el-form-item>

      <el-divider content-position="left">回调入口</el-divider>
      <el-alert
        v-if="form.event_mode === 'websocket'"
        type="info"
        :closable="false"
        show-icon
        style="margin:-4px 0 14px"
      >
        当前为长连接模式，下面的回调地址与来源 IP 白名单都不生效（回调入口已停用）。
      </el-alert>
      <el-form-item label="事件订阅地址">
        <div class="url-row">
          <el-input :model-value="app.event_url" readonly />
          <el-button :icon="CopyDocument" @click="copy(app.event_url)">复制</el-button>
        </div>
      </el-form-item>
      <el-form-item label="卡片回调地址">
        <div class="url-row">
          <el-input :model-value="app.card_url" readonly />
          <el-button :icon="CopyDocument" @click="copy(app.card_url)">复制</el-button>
        </div>
        <div class="tip">
          两条地址填到飞书开放平台的「事件订阅」与「卡片请求网址」。
          地址里的随机串等于一层共享密钥，轮换后旧地址立即失效。
        </div>
      </el-form-item>
      <el-form-item label=" ">
        <el-button type="warning" plain :icon="Refresh" @click="handleRotate">
          轮换回调地址
        </el-button>
      </el-form-item>

      <el-divider content-position="left">来源限制</el-divider>
      <el-form-item label="不限制来源 IP">
        <el-switch v-model="form.ip_unrestricted" />
        <span class="tip inline">留空白名单**不等于**不限制，必须显式勾选</span>
      </el-form-item>
      <el-form-item label="回调来源 IP 白名单">
        <el-input
          v-model="form.ip_whitelist"
          type="textarea"
          :rows="3"
          :disabled="form.ip_unrestricted"
          placeholder="飞书开放平台公布的回调出口 IP 或 CIDR，逗号/换行分隔"
        />
        <div class="tip">
          这是应用层白名单；nginx 侧还有一份（<code>deploy/nginx/conf/snippets/feishu_callback_ips.conf</code>），
          两层都要放行。
        </div>
      </el-form-item>

      <el-divider content-position="left">开关</el-divider>
      <el-form-item label="启用飞书接入">
        <el-switch v-model="form.enabled" />
      </el-form-item>
      <el-form-item label="启用飞书登录">
        <el-switch v-model="form.sso_enabled" :disabled="!form.enabled" />
        <span class="tip inline">飞书登录视为已过二次认证；sudo 仍要验证码/密码</span>
      </el-form-item>
      <el-form-item label="飞书登录回调地址">
        <el-input
          v-model="form.sso_redirect_uri"
          :placeholder="`留空则按当前域名推导：${defaultRedirect}`"
        />
        <div class="tip">须与飞书后台「重定向 URL」白名单一致。</div>
      </el-form-item>
      <el-form-item label="允许单聊发起执行">
        <el-switch v-model="form.allow_p2p_execute" />
        <span class="tip inline">
          机器人自定义菜单、私聊 @机器人 都走这个开关；群里仍按会话白名单
        </span>
      </el-form-item>
      <el-form-item label="双人复核">
        <el-radio-group v-model="form.dual_review">
          <el-radio value="off">关闭</el-radio>
          <el-radio value="prod">仅生产环境</el-radio>
          <el-radio value="all">所有任务</el-radio>
        </el-radio-group>
        <div class="tip">
          开启后，发起人确认之后还要<b>另一个</b>有生产执行权限的人点「复核通过」才真执行。
          拦的是「一个人在错误的时间对着错误的环境点了确认」。
          <b>只在群里可用</b>——单聊里那张卡只有发起人看得见，没有第二个人能点。
        </div>
      </el-form-item>
      <el-form-item label="sudo 卡片确认">
        <el-radio-group v-model="form.sudo_confirm">
          <el-radio value="off">关闭</el-radio>
          <el-radio value="fallback">仅未绑验证器</el-radio>
          <el-radio value="all">所有人</el-radio>
        </el-radio-group>
        <div class="tip">
          敏感操作二次验证时，允许在飞书上点一下代替输验证码。
          默认只兜底给没有其他手段的账号（飞书自动建号的没有可用密码）——
          卡片确认摩擦小，容易养成随手点的习惯，而 MFA fatigue 正是靠这个得手。
        </div>
      </el-form-item>
      <el-form-item label="未绑定时自动建号">
        <el-switch v-model="form.auto_create_user" @change="onAutoCreateChange" />
        <span class="tip inline danger" v-if="form.auto_create_user">
          开启后任何飞书成员登录即自动获得平台账号（只读、无执行能力）
        </span>
      </el-form-item>
      <el-form-item label="备注">
        <el-input v-model="form.remark" maxlength="200" />
      </el-form-item>

      <el-form-item>
        <el-button type="primary" :loading="submitting" @click="handleSave">保存</el-button>
        <el-button @click="loadApp">重置</el-button>
      </el-form-item>
    </el-form>
  </el-card>

  <!-- ── 会话白名单 ───────────────────────────────────── -->
  <el-card shadow="never">
    <template #header>
      <div class="card-header">
        <span>会话白名单</span>
        <span class="tip">
          只有登记的会话能收通知；「允许发起执行」还要再单独勾选。
          未登记的会话一律不渲染按钮。
        </span>
      </div>
    </template>

    <div class="toolbar">
      <el-input
        v-model="keyword"
        placeholder="搜索会话名称 / ID"
        clearable
        :prefix-icon="Search"
        style="width:240px"
        @change="loadChannels"
      />
      <el-button type="primary" :icon="Plus" @click="openChannel()">新增会话</el-button>
    </div>

    <el-table v-loading="channelLoading" :data="channels" stripe>
      <el-table-column prop="name" label="会话名称" min-width="160" />
      <el-table-column prop="chat_id" label="会话 ID" min-width="200" show-overflow-tooltip />
      <el-table-column prop="chat_type_display" label="类型" width="90" />
      <el-table-column label="允许执行" width="100">
        <template #default="{ row }">
          <el-tag :type="row.allow_execute ? 'danger' : 'info'" size="small">
            {{ row.allow_execute ? '允许' : '禁止' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="播报" min-width="180">
        <template #default="{ row }">
          <template v-if="row.notify_enabled">
            <el-tag :type="row.notify_scope === 'all' ? 'warning' : 'success'" size="small">
              {{ row.notify_scope_display }}
            </el-tag>
            <span class="tip" style="margin-left:6px">
              {{ [row.notify_project_group_name, row.notify_environment_name]
                   .filter(Boolean).join(' / ') || '不限维度' }}
            </span>
          </template>
          <el-tag v-else type="info" size="small">关闭</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="90">
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
      <el-table-column label="操作" width="150" fixed="right">
        <template #default="{ row }">
          <el-button size="small" :icon="Edit" @click="openChannel(row)">编辑</el-button>
          <el-button size="small" type="danger" :icon="Delete" @click="removeChannel(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>
    <el-pagination
      v-model:current-page="page"
      v-model:page-size="pageSize"
      :page-sizes="[20, 50, 100]"
      :total="total"
      layout="total, sizes, prev, pager, next"
      style="margin-top:12px;justify-content:flex-end"
      @size-change="loadChannels"
      @current-change="loadChannels"
    />
  </el-card>

  <el-dialog v-model="channelVisible" :title="editRow ? '编辑会话' : '新增会话'" width="520px">
    <el-form ref="channelFormRef" :model="channelForm" :rules="channelRules" label-width="120px">
      <el-form-item label="会话 ID" prop="chat_id">
        <el-input v-model="channelForm.chat_id" placeholder="oc_xxx（群）/ ou_xxx（个人）" />
        <div class="tip">
          群 ID 用 <code>manage.py feishu_check --chats</code> 列出（飞书界面上不显示）；
          个人订阅直接填对方的 open_id。
        </div>
      </el-form-item>
      <el-form-item label="会话名称" prop="name">
        <el-input v-model="channelForm.name" maxlength="100" />
      </el-form-item>
      <el-form-item label="类型">
        <el-radio-group v-model="channelForm.chat_type">
          <el-radio value="group">群聊</el-radio>
          <el-radio value="p2p">单聊</el-radio>
        </el-radio-group>
      </el-form-item>
      <el-form-item label="接收通知">
        <el-switch v-model="channelForm.notify_enabled" />
      </el-form-item>
      <template v-if="channelForm.notify_enabled">
        <el-form-item label="播报范围">
          <el-radio-group v-model="channelForm.notify_scope">
            <el-radio value="failed">仅失败</el-radio>
            <el-radio value="all">全部结果</el-radio>
          </el-radio-group>
          <div class="tip">
            默认只播失败：成功的执行每天几十上百次，全播会把群变成噪声源，
            真正该被看见的失败反而被划过去。
          </div>
        </el-form-item>
        <el-form-item label="仅播报项目组">
          <el-select v-model="channelForm.notify_project_group" clearable
                     placeholder="不限" style="width:100%">
            <el-option v-for="d in dictOptions.projects" :key="d.id"
                       :label="d.name" :value="d.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="仅播报环境">
          <el-select v-model="channelForm.notify_environment" clearable
                     placeholder="不限" style="width:100%">
            <el-option v-for="d in dictOptions.envs" :key="d.id"
                       :label="d.name" :value="d.id" />
          </el-select>
        </el-form-item>
      </template>
      <el-form-item label="允许发起执行">
        <el-switch v-model="channelForm.allow_execute" />
        <span class="tip inline danger" v-if="channelForm.allow_execute">
          该会话内可直接发起任务执行，鉴权仍按点按钮的人判
        </span>
      </el-form-item>
      <el-form-item label="启用">
        <el-switch v-model="channelForm.enabled" />
      </el-form-item>
      <el-form-item label="备注">
        <el-input v-model="channelForm.remark" maxlength="200" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="channelVisible = false">取消</el-button>
      <el-button type="primary" :loading="channelSubmitting" @click="saveChannel">保存</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  CopyDocument, Delete, Edit, Plus, Refresh, Search,
} from '@element-plus/icons-vue'
import {
  createChannel, deleteChannel, getApp, getChannels,
  rotateCallbackSlug, updateApp, updateChannel,
} from '../../api/feishu'
import { environmentApi, fetchDictOptions, projectGroupApi } from '../../api/assets'

const secretFields = [
  { key: 'app_secret', has: 'has_app_secret', label: 'App Secret', placeholder: '飞书应用凭证' },
  { key: 'encrypt_key', has: 'has_encrypt_key', label: 'Encrypt Key', placeholder: '事件加密密钥' },
  { key: 'verification_token', has: 'has_verification_token', label: 'Verification Token', placeholder: '事件校验令牌' },
]

const loading = ref(false)
const submitting = ref(false)
const app = ref({})
const form = reactive({
  app_id: '', app_secret: '', encrypt_key: '', verification_token: '',
  ip_unrestricted: false, ip_whitelist: '',
  event_mode: 'callback',
  enabled: false, sso_enabled: false, sso_redirect_uri: '',
  allow_p2p_execute: false, auto_create_user: false, remark: '',
  sudo_confirm: 'fallback', dual_review: 'off',
})

const defaultRedirect = computed(() => `${window.location.origin}/login/feishu`)

async function loadApp() {
  loading.value = true
  try {
    const res = await getApp()
    app.value = res.data
    // 秘密字段一律回填空串：后端把留空视为「不修改」
    Object.assign(form, {
      app_id: res.data.app_id || '',
      event_mode: res.data.event_mode || 'callback',
      app_secret: '', encrypt_key: '', verification_token: '',
      ip_unrestricted: res.data.ip_unrestricted,
      ip_whitelist: res.data.ip_whitelist || '',
      enabled: res.data.enabled,
      sso_enabled: res.data.sso_enabled,
      sso_redirect_uri: res.data.sso_redirect_uri || '',
      allow_p2p_execute: res.data.allow_p2p_execute,
      sudo_confirm: res.data.sudo_confirm || 'fallback',
      dual_review: res.data.dual_review || 'off',
      auto_create_user: res.data.auto_create_user,
      remark: res.data.remark || '',
    })
  } finally {
    loading.value = false
  }
}

async function handleSave() {
  submitting.value = true
  try {
    const res = await updateApp({ ...form })
    app.value = res.data
    form.app_secret = form.encrypt_key = form.verification_token = ''
    ElMessage.success('已保存')
  } finally {
    submitting.value = false
  }
}

async function onAutoCreateChange(value) {
  if (!value) return
  try {
    await ElMessageBox.confirm(
      '开启后，任何能通过飞书登录的成员都会自动获得平台账号（只读、无执行能力、无可用密码）。'
      + '确认要开启吗？',
      '开启自动建号',
      { type: 'warning', confirmButtonText: '确认开启' },
    )
  } catch {
    form.auto_create_user = false
  }
}

async function handleRotate() {
  try {
    await ElMessageBox.confirm(
      '轮换后旧的事件订阅与卡片回调地址立即失效，必须到飞书开放平台同步更新，'
      + '否则飞书侧的所有回调都会被拒。确认轮换吗？',
      '轮换回调地址',
      { type: 'warning', confirmButtonText: '确认轮换' },
    )
  } catch {
    return
  }
  const res = await rotateCallbackSlug()
  app.value = res.data
  ElMessage.success('已轮换，请到飞书开放平台更新回调地址')
}

async function copy(text) {
  if (!text) return
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('已复制')
  } catch {
    // http 页面或浏览器拒绝时拿不到剪贴板，退回让用户自己选中
    ElMessage.warning('浏览器不允许自动复制，请手动选中地址复制')
  }
}

// ── 会话白名单 ──
const channels = ref([])
const channelLoading = ref(false)
const keyword = ref('')
const page = ref(1)
const pageSize = ref(20)
const total = ref(0)
const channelVisible = ref(false)
const channelSubmitting = ref(false)
const channelFormRef = ref()
const editRow = ref(null)
const channelForm = reactive({
  chat_id: '', name: '', chat_type: 'group',
  allow_execute: false, notify_enabled: true, enabled: true, remark: '',
  notify_scope: 'failed', notify_project_group: null, notify_environment: null,
})
const dictOptions = reactive({ projects: [], envs: [] })
const channelRules = {
  chat_id: [{ required: true, message: '请输入会话 ID', trigger: 'blur' }],
  name: [{ required: true, message: '请输入会话名称', trigger: 'blur' }],
}

async function loadChannels() {
  channelLoading.value = true
  try {
    const res = await getChannels({
      keyword: keyword.value || undefined,
      page: page.value,
      page_size: pageSize.value,
    })
    channels.value = res.data.results
    total.value = res.data.total
  } finally {
    channelLoading.value = false
  }
}

function openChannel(row = null) {
  editRow.value = row
  Object.assign(channelForm, row ? {
    chat_id: row.chat_id, name: row.name, chat_type: row.chat_type,
    allow_execute: row.allow_execute, notify_enabled: row.notify_enabled,
    enabled: row.enabled, remark: row.remark || '',
    notify_scope: row.notify_scope || 'failed',
    notify_project_group: row.notify_project_group || null,
    notify_environment: row.notify_environment || null,
  } : {
    chat_id: '', name: '', chat_type: 'group',
    allow_execute: false, notify_enabled: true, enabled: true, remark: '',
    notify_scope: 'failed', notify_project_group: null, notify_environment: null,
  })
  channelVisible.value = true
}

async function saveChannel() {
  await channelFormRef.value.validate()
  channelSubmitting.value = true
  try {
    if (editRow.value) {
      await updateChannel(editRow.value.id, { ...channelForm })
    } else {
      await createChannel({ ...channelForm })
    }
    ElMessage.success('已保存')
    channelVisible.value = false
    loadChannels()
  } finally {
    channelSubmitting.value = false
  }
}

async function removeChannel(row) {
  await ElMessageBox.confirm(
    `删除后「${row.name}」将收不到任何通知，也无法从该会话发起执行。确认删除吗？`,
    '删除会话', { type: 'warning' },
  )
  await deleteChannel(row.id)
  ElMessage.success('已删除')
  loadChannels()
}

async function loadDicts() {
  const [projects, envs] = await Promise.all([
    fetchDictOptions(projectGroupApi),
    fetchDictOptions(environmentApi),
  ])
  dictOptions.projects = projects.data.results
  dictOptions.envs = envs.data.results
}

onMounted(() => {
  loadApp()
  loadChannels()
  loadDicts()
})
</script>

<style scoped>
.card-header {
  display: flex;
  align-items: center;
  gap: 10px;
}
.toolbar {
  display: flex;
  justify-content: space-between;
  margin-bottom: 12px;
}
.url-row {
  display: flex;
  gap: 8px;
  width: 100%;
}
.tip {
  font-size: 12px;
  color: #909399;
  line-height: 1.6;
}
.tip.inline {
  margin-left: 10px;
}
.tip.danger {
  color: #E6A23C;
}
.unset {
  color: #C0C4CC;
}
</style>
