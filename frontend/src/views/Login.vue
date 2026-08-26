<template>
  <div class="login-page">
    <el-card class="login-card">
      <template #header>
        <div class="login-header">
          <el-icon size="32" color="#409EFF"><Monitor /></el-icon>
          <h2>运维管理后台</h2>
          <p>OpsManager</p>
        </div>
      </template>
      <!-- 第一步：用户名密码 -->
      <el-form
        v-if="step === 'password'"
        ref="formRef"
        :model="form"
        :rules="rules"
        size="large"
        @keyup.enter="handleLogin"
      >
        <el-form-item prop="username">
          <el-input
            v-model="form.username"
            name="username"
            autocomplete="username"
            placeholder="用户名"
            :prefix-icon="User"
            clearable
          />
        </el-form-item>
        <el-form-item prop="password">
          <el-input
            v-model="form.password"
            type="password"
            name="password"
            autocomplete="current-password"
            placeholder="密码"
            :prefix-icon="Lock"
            show-password
          />
        </el-form-item>
        <el-form-item>
          <el-button
            type="primary"
            :loading="loading"
            style="width: 100%"
            @click="handleLogin"
          >
            登 录
          </el-button>
        </el-form-item>
      </el-form>

      <!-- 第二步：验证器验证码（可切换为恢复码） -->
      <el-form v-else size="large" @keyup.enter="handleVerify">
        <el-alert type="info" :closable="false" show-icon style="margin-bottom:16px">
          {{ useRecovery ? '输入一个未使用过的恢复码' : '输入验证器 App 上当前显示的 6 位验证码' }}
        </el-alert>
        <p class="mfa-account">验证账号：<strong>{{ form.username }}</strong></p>
        <!-- 只读用户名：留在 DOM 里让密码管理器把本页与已保存条目对上，
             否则它拿不准该填哪个账号的一次性密码。aria-hidden + 视觉隐藏，
             不进 tab 序列，读屏器也不会念第二遍（上面已有可见文案） -->
        <input
          class="visually-hidden"
          type="text"
          name="username"
          autocomplete="username"
          tabindex="-1"
          aria-hidden="true"
          readonly
          :value="form.username"
        />
        <el-form-item>
          <el-input
            ref="codeRef"
            v-model="code"
            :name="useRecovery ? 'recovery-code' : 'otp'"
            :autocomplete="useRecovery ? 'off' : 'one-time-code'"
            :inputmode="useRecovery ? 'text' : 'numeric'"
            :pattern="useRecovery ? undefined : '[0-9]*'"
            :placeholder="useRecovery ? '恢复码，如 ABCD-EFGH-JKMN' : '6 位验证码'"
            :prefix-icon="Key"
            :maxlength="useRecovery ? 20 : 6"
            enterkeyhint="done"
            clearable
            @input="onCodeInput"
          />
        </el-form-item>
        <el-form-item>
          <el-button
            type="primary"
            :loading="loading"
            style="width: 100%"
            @click="handleVerify"
          >
            验 证
          </el-button>
        </el-form-item>
        <div class="mfa-actions">
          <el-link type="primary" :underline="false" @click="toggleRecovery">
            {{ useRecovery ? '用验证器验证码' : '验证器丢了？用恢复码' }}
          </el-link>
          <el-link :underline="false" @click="backToPassword">返回</el-link>
        </div>
      </el-form>
    </el-card>
  </div>
</template>

<script setup>
import { ref, reactive, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { User, Lock, Monitor, Key } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '../stores/auth'

const router = useRouter()
const authStore = useAuthStore()

const formRef = ref()
const codeRef = ref()
const loading = ref(false)
// password → mfa：已绑定 MFA 的账号，密码步只换到一张 5 分钟的挑战票据
const step = ref('password')
const useRecovery = ref(false)
const code = ref('')
const form = reactive({ username: '', password: '' })

const rules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
}

async function handleLogin() {
  await formRef.value.validate()
  loading.value = true
  try {
    const res = await authStore.login(form)
    if (res.mfaRequired) {
      step.value = 'mfa'
      code.value = ''
      await nextTick()
      codeRef.value?.focus()
      return
    }
    afterLogin()
  } finally {
    loading.value = false
  }
}

async function handleVerify() {
  if (!code.value.trim()) {
    ElMessage.warning(useRecovery.value ? '请输入恢复码' : '请输入验证码')
    return
  }
  loading.value = true
  try {
    const res = await authStore.verifyMfa(code.value.trim())
    if (res.recoveryCodesLeft !== undefined) {
      ElMessage.warning(`已使用一个恢复码，剩余 ${res.recoveryCodesLeft} 个`)
    }
    afterLogin()
  } catch (err) {
    code.value = ''
    // 票据过期/用尽次数时后端返回 401，退回密码步重新开始
    if (err?.response?.status === 401) {
      backToPassword()
    }
  } finally {
    loading.value = false
  }
}

function afterLogin() {
  ElMessage.success('登录成功')
  // 强制策略下未绑定 MFA 的账号先去绑定页，路由守卫也会兜住
  router.push(authStore.mfaEnrollmentRequired ? { name: 'SecurityMfa' } : '/')
}

function toggleRecovery() {
  useRecovery.value = !useRecovery.value
  code.value = ''
  nextTick(() => codeRef.value?.focus())
}

function backToPassword() {
  authStore.cancelMfa()
  step.value = 'password'
  useRecovery.value = false
  code.value = ''
  form.password = ''
}

function onCodeInput(value) {
  if (useRecovery.value) return
  // 只留数字：密码管理器/短信填充常带空格或 "123 456" 这类分组
  code.value = String(value).replace(/\D/g, '').slice(0, 6)
  // 满 6 位自动提交（省一次点击，输错也能立刻重来）。1Password 一次性填满
  // 时同样走这里，填完即验证，不用再点按钮
  if (code.value.length === 6 && !loading.value) {
    handleVerify()
  }
}
</script>

<style scoped>
.login-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
}
.login-card {
  width: 400px;
  border-radius: 12px;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
}
.login-header {
  text-align: center;
  padding: 8px 0;
}
.login-header h2 {
  margin: 8px 0 4px;
  font-size: 22px;
  color: #303133;
}
.login-header p {
  margin: 0;
  color: #909399;
  font-size: 13px;
}
.mfa-actions {
  display: flex;
  justify-content: space-between;
  font-size: 13px;
}
.mfa-account {
  margin: 0 0 12px;
  font-size: 13px;
  color: #606266;
}
/* 给密码管理器看的只读用户名：不能用 display:none / hidden——那样多数扩展
   会跳过它，也就关联不上条目 */
.visually-hidden {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
  border: 0;
}
</style>
