<template>
  <!-- 全局挂载（App.vue）：任何页面触发受控操作都用这一个框 -->
  <el-dialog
    v-model="visible"
    title="二次验证"
    width="420px"
    :close-on-click-modal="false"
    @closed="onClosed"
  >
    <el-alert type="info" :closable="false" show-icon style="margin-bottom:14px">
      该操作会修改配置类数据（凭据 / 用户 / Agent / 云账号 / 系统配置），
      需要再确认一次是你本人。验证通过后 {{ ttlMinutes }} 分钟内同类操作免重验。
    </el-alert>
    <el-form v-loading="sudoState.loading" label-width="80px" @submit.prevent>
      <el-form-item v-if="sudoState.mfaEnabled" label="验证码">
        <el-input
          ref="inputRef"
          v-model="code"
          name="otp"
          autocomplete="one-time-code"
          inputmode="numeric"
          pattern="[0-9]*"
          maxlength="6"
          placeholder="验证器上当前的 6 位码"
          @input="onCodeInput"
          @keyup.enter="submit"
        />
      </el-form-item>
      <el-form-item v-else label="登录密码">
        <el-input
          ref="inputRef"
          v-model="password"
          type="password"
          name="password"
          autocomplete="current-password"
          show-password
          placeholder="当前登录密码"
          @keyup.enter="submit"
        />
      </el-form-item>
    </el-form>
    <!-- 飞书确认：给「既没验证器、也没有可用密码」的账号一条路
         （飞书登录自动创建的账号就是这种）。等待期间禁掉表单，避免两条路同时走 -->
    <div v-if="feishu.available" class="feishu-row">
      <el-divider class="feishu-divider">或</el-divider>
      <el-button
        style="width:100%"
        :loading="feishu.waiting"
        :disabled="sudoState.submitting"
        @click="startFeishu"
      >
        <el-icon style="margin-right:6px"><ChatDotRound /></el-icon>
        {{ feishu.waiting ? `已发到飞书，等待确认…（${feishu.left}s）` : '用飞书确认' }}
      </el-button>
      <div v-if="feishu.waiting" class="feishu-tip">
        在飞书上点「确认是我操作的」。
        <b>不是你发起的就点「不是我」</b>，并立即修改密码。
      </div>
    </div>

    <template #footer>
      <el-button @click="cancel">取消</el-button>
      <el-button type="primary" :loading="sudoState.submitting" @click="submit">
        验证
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { ref, reactive, computed, watch, nextTick, onUnmounted } from 'vue'
import { ElMessage } from 'element-plus'
import { ChatDotRound } from '@element-plus/icons-vue'
import { getSudoStatus, grantSudo } from '../api/auth'
import { getFeishuSudo, requestFeishuSudo } from '../api/feishu'
import { sudoState, settleSudo, markGranted } from '../utils/sudo'

const code = ref('')
const password = ref('')
// 已提交过的码：填满自动提交后用户再回车/点按钮，第二次拿同一个码去校验必然
// 撞后端防重放，报「已使用过」却其实已经放行——最费解的一种错
let lastSubmitted = ''
const inputRef = ref()
const ttl = ref(600)
// 飞书确认：available 由后端判（未绑定 / 已绑验证器 / 未开启都会关掉这条路）
const feishu = reactive({ available: false, waiting: false, left: 0 })
let feishuTimer = null

const visible = computed({
  get: () => sudoState.visible,
  set: (v) => { if (!v) cancel() },
})
const ttlMinutes = computed(() => Math.max(1, Math.round(ttl.value / 60)))

watch(() => sudoState.visible, async (open) => {
  if (!open) return
  code.value = ''
  password.value = ''
  lastSubmitted = ''
  sudoState.loading = true
  try {
    // 权威口径来自后端：本地缓存的 user 可能是绑定 MFA 之前的
    const data = (await getSudoStatus()).data
    sudoState.mfaEnabled = !!data.mfa_enabled
    ttl.value = data.ttl || ttl.value
  } catch {
    // 查不到就按密码确认走，后端仍会按真实状态校验
    sudoState.mfaEnabled = false
  } finally {
    sudoState.loading = false
  }
  stopFeishu()
  try {
    feishu.available = !!(await getFeishuSudo()).data?.available
  } catch {
    feishu.available = false
  }
  await nextTick()
  inputRef.value?.focus()
})

async function startFeishu() {
  feishu.waiting = true
  let token = ''
  try {
    const res = await requestFeishuSudo({ action: sudoState.action || '配置面写操作' })
    token = res.data.token
    feishu.left = res.data.expires_in || 120
  } catch {
    feishu.waiting = false
    return
  }
  // 轮询而不是等推送：这条链路上没有 WebSocket，而 2 秒一次、最多两分钟的开销，
  // 远小于为它单拉一条长连接
  feishuTimer = setInterval(async () => {
    feishu.left -= 2
    if (feishu.left <= 0) {
      stopFeishu()
      ElMessage.warning('飞书确认已超时，请重试')
      return
    }
    let data
    try {
      data = (await getFeishuSudo({ token })).data
    } catch {
      stopFeishu()          // 票据失效/会话变了，后端已给出提示
      return
    }
    if (data.state !== 'granted') return
    stopFeishu()
    ttl.value = data.expires_in || ttl.value
    markGranted(ttl.value)
    settleSudo(true)
  }, 2000)
}

function stopFeishu() {
  if (feishuTimer) clearInterval(feishuTimer)
  feishuTimer = null
  feishu.waiting = false
  feishu.left = 0
}

onUnmounted(stopFeishu)

function onCodeInput(value) {
  // 只留数字（管理器填充常带空格），满 6 位自动提交
  code.value = String(value).replace(/\D/g, '').slice(0, 6)
  if (code.value.length === 6) submit()
}

async function submit() {
  if (sudoState.submitting || !sudoState.visible) return
  const payload = sudoState.mfaEnabled
    ? { code: code.value.trim() }
    : { password: password.value }
  const value = sudoState.mfaEnabled ? payload.code : payload.password
  if (!value) {
    ElMessage.warning(sudoState.mfaEnabled ? '请输入验证码' : '请输入登录密码')
    return
  }
  if (sudoState.mfaEnabled && value === lastSubmitted) return
  lastSubmitted = value
  sudoState.submitting = true
  try {
    const res = await grantSudo(payload)
    ttl.value = res.data?.expires_in || ttl.value
    markGranted(ttl.value)    // 顶栏显示剩余时间
    settleSudo(true)          // 拦截器据此重放原请求
  } catch {
    // 失败原因由拦截器统一提示（密码错/验证码错/超频），留在框里让人重试
    code.value = ''
    lastSubmitted = ''
    sudoState.submitting = false
  }
}

function cancel() {
  stopFeishu()
  settleSudo(false)
}

function onClosed() {
  if (sudoState.visible) return
  sudoState.submitting = false
}
</script>

<style scoped>
.feishu-row {
  margin-top: -6px;
}
.feishu-divider {
  margin: 4px 0 12px;
}
.feishu-divider :deep(.el-divider__text) {
  color: #909399;
  font-size: 12px;
}
.feishu-tip {
  margin-top: 8px;
  font-size: 12px;
  color: #909399;
  line-height: 1.6;
}
</style>
