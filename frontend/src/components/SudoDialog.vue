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
    <template #footer>
      <el-button @click="cancel">取消</el-button>
      <el-button type="primary" :loading="sudoState.submitting" @click="submit">
        验证
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { ref, computed, watch, nextTick } from 'vue'
import { ElMessage } from 'element-plus'
import { getSudoStatus, grantSudo } from '../api/auth'
import { sudoState, settleSudo } from '../utils/sudo'

const code = ref('')
const password = ref('')
const inputRef = ref()
const ttl = ref(600)

const visible = computed({
  get: () => sudoState.visible,
  set: (v) => { if (!v) cancel() },
})
const ttlMinutes = computed(() => Math.max(1, Math.round(ttl.value / 60)))

watch(() => sudoState.visible, async (open) => {
  if (!open) return
  code.value = ''
  password.value = ''
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
  await nextTick()
  inputRef.value?.focus()
})

function onCodeInput(value) {
  // 只留数字（管理器填充常带空格），满 6 位自动提交
  code.value = String(value).replace(/\D/g, '').slice(0, 6)
  if (code.value.length === 6 && !sudoState.submitting) submit()
}

async function submit() {
  const payload = sudoState.mfaEnabled
    ? { code: code.value.trim() }
    : { password: password.value }
  const value = sudoState.mfaEnabled ? payload.code : payload.password
  if (!value) {
    ElMessage.warning(sudoState.mfaEnabled ? '请输入验证码' : '请输入登录密码')
    return
  }
  sudoState.submitting = true
  try {
    const res = await grantSudo(payload)
    ttl.value = res.data?.expires_in || ttl.value
    settleSudo(true)          // 拦截器据此重放原请求
  } catch {
    // 失败原因由拦截器统一提示（密码错/验证码错/超频），留在框里让人重试
    code.value = ''
    sudoState.submitting = false
  }
}

function cancel() {
  settleSudo(false)
}

function onClosed() {
  if (sudoState.visible) return
  sudoState.submitting = false
}
</script>
