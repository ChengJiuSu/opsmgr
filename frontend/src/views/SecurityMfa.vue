<template>
  <div>
    <el-alert
      v-if="status.enforced && !status.enabled"
      type="warning"
      :closable="false"
      show-icon
      style="margin-bottom:12px"
    >
      按安全策略（<code>mfa.enforce_scope = {{ status.enforce_scope }}</code>）你必须绑定二次认证，
      完成绑定后才能访问其他功能。
    </el-alert>

    <el-card v-loading="loading">
      <template #header>
        <div class="card-header">
          <span>二次认证（MFA）</span>
          <el-tag :type="status.enabled ? 'success' : 'info'" size="small">
            {{ status.enabled ? '已启用' : (status.pending ? '待激活' : '未绑定') }}
          </el-tag>
        </div>
      </template>

      <!-- 已启用：展示状态与管理操作 -->
      <template v-if="status.enabled && !enrolling">
        <el-descriptions :column="2" border>
          <el-descriptions-item label="启用时间">
            {{ formatTime(status.activated_at) }}
          </el-descriptions-item>
          <el-descriptions-item label="最近验证">
            {{ formatTime(status.last_used_at) || '—' }}
          </el-descriptions-item>
          <el-descriptions-item label="剩余恢复码">
            <el-tag :type="status.recovery_codes_left <= 2 ? 'danger' : 'info'" size="small">
              {{ status.recovery_codes_left }} 个
            </el-tag>
            <span v-if="status.recovery_codes_left <= 2" class="tip">
              建议重新生成一批
            </span>
          </el-descriptions-item>
          <el-descriptions-item label="强制策略">
            {{ status.enforced ? '本账号必须启用' : '自愿启用' }}
          </el-descriptions-item>
        </el-descriptions>
        <div class="actions">
          <el-button @click="openSensitive('codes')">重新生成恢复码</el-button>
          <el-button type="danger" plain @click="openSensitive('disable')">解除绑定</el-button>
        </div>
      </template>

      <!-- 未绑定：说明 + 开始绑定 -->
      <template v-else-if="!enrolling">
        <p class="intro">
          启用后登录需要两步：密码 + 验证器 App 上的 6 位动态码。
          支持任意兼容 TOTP 的验证器（Google Authenticator、Microsoft Authenticator、1Password 等）。
          修改凭据、用户、Agent、云账号、系统配置这类配置数据时也会要求再验一次
          （验过 {{ sudoMinutes }} 分钟内免重验）。
        </p>
        <el-button type="primary" @click="startEnroll">开始绑定</el-button>
      </template>

      <!-- 绑定流程：扫码 → 输码激活 -->
      <template v-else>
        <el-steps :active="enroll.codesConfirmed ? 1 : 0" simple style="margin-bottom:18px">
          <el-step title="保存恢复码" />
          <el-step title="扫码并激活" />
        </el-steps>

        <template v-if="!enroll.codesConfirmed">
          <el-alert type="error" :closable="false" show-icon style="margin-bottom:12px">
            以下恢复码<strong>只显示这一次</strong>。验证器丢了时用它登录，每个只能用一次。
            请立刻复制或下载保存到安全的地方。
          </el-alert>
          <div class="codes">
            <span v-for="c in enroll.recoveryCodes" :key="c" class="code">{{ c }}</span>
          </div>
          <div class="actions">
            <el-button @click="copyCodes">复制</el-button>
            <el-button @click="downloadCodes">下载 txt</el-button>
            <el-button type="primary" @click="enroll.codesConfirmed = true">
              已保存，下一步
            </el-button>
          </div>
        </template>

        <template v-else>
          <div class="enroll-body">
            <div class="qr-box">
              <img v-if="enroll.qr" :src="enroll.qr" alt="MFA 二维码" width="200" height="200" />
              <p class="tip">用验证器 App 扫码</p>
            </div>
            <div class="manual">
              <p>无法扫码时手动输入密钥：</p>
              <el-input :model-value="enroll.secret" readonly>
                <template #append>
                  <el-button @click="copyText(enroll.secret, '密钥已复制')">复制</el-button>
                </template>
              </el-input>
              <p class="tip">类型 TOTP、SHA1、6 位、30 秒（各 App 默认值）</p>
              <el-form label-width="90px" style="margin-top:14px" @submit.prevent>
                <el-form-item label="验证码">
                  <el-input
                    v-model="enroll.code"
                    name="otp"
                    autocomplete="one-time-code"
                    inputmode="numeric"
                    pattern="[0-9]*"
                    placeholder="App 上当前的 6 位码"
                    maxlength="6"
                    @input="onEnrollCode"
                    @keyup.enter="activate"
                  />
                </el-form-item>
              </el-form>
              <div class="actions">
                <el-button @click="cancelEnroll">取消</el-button>
                <el-button type="primary" :loading="submitting" @click="activate">
                  验证并启用
                </el-button>
              </div>
              <p class="tip">
                验证码不对时先检查手机时间是否准确——TOTP 靠时间同步，差超过 30 秒就算不上。
              </p>
            </div>
          </div>
        </template>
      </template>
    </el-card>

    <!-- 敏感操作：密码 + 当前验证码双确认 -->
    <el-dialog v-model="sensitive.visible" :title="sensitive.title" width="420px">
      <el-alert v-if="sensitive.action === 'disable'" type="warning" :closable="false" show-icon
                style="margin-bottom:12px">
        解绑后登录只需密码。若安全策略要求必须启用，解绑后你会被锁在绑定页。
      </el-alert>
      <el-alert v-else type="info" :closable="false" show-icon style="margin-bottom:12px">
        重新生成会作废现有全部恢复码。
      </el-alert>
      <el-form :model="sensitive.form" label-width="90px">
        <el-form-item label="登录密码">
          <el-input
            v-model="sensitive.form.password"
            type="password"
            name="password"
            autocomplete="current-password"
            show-password
          />
        </el-form-item>
        <el-form-item label="验证码">
          <el-input
            v-model="sensitive.form.code"
            name="otp"
            autocomplete="one-time-code"
            inputmode="numeric"
            pattern="[0-9]*"
            maxlength="6"
            placeholder="当前 6 位码"
            @input="onSensitiveCode"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="sensitive.visible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submitSensitive">确定</el-button>
      </template>
    </el-dialog>

    <!-- 重新生成后的新恢复码 -->
    <el-dialog v-model="newCodes.visible" title="新的恢复码" width="460px">
      <el-alert type="error" :closable="false" show-icon style="margin-bottom:12px">
        只显示这一次，旧恢复码已全部作废。
      </el-alert>
      <div class="codes">
        <span v-for="c in newCodes.list" :key="c" class="code">{{ c }}</span>
      </div>
      <template #footer>
        <el-button @click="copyText(newCodes.list.join('\n'), '恢复码已复制')">复制</el-button>
        <el-button type="primary" @click="newCodes.visible = false">我已保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import QRCode from 'qrcode'
import { ElMessage } from 'element-plus'
import {
  getMfaStatus, mfaSetup, mfaActivate, mfaDisable, mfaRegenerateCodes,
} from '../api/auth'
import { useAuthStore } from '../stores/auth'

const authStore = useAuthStore()

const loading = ref(false)
const submitting = ref(false)
const enrolling = ref(false)
const status = ref({
  enabled: false, pending: false, recovery_codes_left: 0,
  enforced: false, enforce_scope: 'off', sudo_ttl: 600,
})
const sudoMinutes = computed(() => Math.max(1, Math.round((status.value.sudo_ttl || 600) / 60)))
const enroll = reactive({
  secret: '', uri: '', qr: '', recoveryCodes: [], codesConfirmed: false, code: '',
})
const sensitive = reactive({ visible: false, action: '', title: '', form: { password: '', code: '' } })
const newCodes = reactive({ visible: false, list: [] })

onMounted(fetchStatus)

async function fetchStatus() {
  loading.value = true
  try {
    status.value = (await getMfaStatus()).data
  } finally {
    loading.value = false
  }
}

async function startEnroll() {
  loading.value = true
  try {
    const data = (await mfaSetup()).data
    Object.assign(enroll, {
      secret: data.secret,
      uri: data.uri,
      recoveryCodes: data.recovery_codes,
      codesConfirmed: false,
      code: '',
    })
    // 二维码在前端渲染：后端只给 otpauth URI，不引图像库
    enroll.qr = await QRCode.toDataURL(data.uri, { width: 200, margin: 1 })
    enrolling.value = true
  } finally {
    loading.value = false
  }
}

async function activate() {
  if (enroll.code.trim().length !== 6) {
    ElMessage.warning('请输入 6 位验证码')
    return
  }
  submitting.value = true
  try {
    const res = await mfaActivate({ code: enroll.code.trim() })
    // 后端换发了带 otp 声明的新令牌，否则强制策略下当前会话仍被挡
    const tokens = res.data
    if (tokens?.access) {
      authStore.persistTokens(tokens.access, tokens.refresh)
    }
    authStore.setEnrollmentRequired(false)
    enrolling.value = false
    ElMessage.success('二次认证已启用')
    await Promise.all([fetchStatus(), authStore.fetchMe()])
  } finally {
    submitting.value = false
  }
}

// 密码管理器/手输都可能带空格或 "123 456" 分组，统一只留数字
function onEnrollCode(value) {
  enroll.code = String(value).replace(/\D/g, '').slice(0, 6)
}

function onSensitiveCode(value) {
  sensitive.form.code = String(value).replace(/\D/g, '').slice(0, 6)
}

function cancelEnroll() {
  enrolling.value = false
  enroll.code = ''
}

function openSensitive(action) {
  sensitive.action = action
  sensitive.title = action === 'disable' ? '解除 MFA 绑定' : '重新生成恢复码'
  sensitive.form.password = ''
  sensitive.form.code = ''
  sensitive.visible = true
}

async function submitSensitive() {
  const payload = { password: sensitive.form.password, code: sensitive.form.code.trim() }
  if (!payload.password || !payload.code) {
    ElMessage.warning('请输入密码与验证码')
    return
  }
  submitting.value = true
  try {
    if (sensitive.action === 'disable') {
      await mfaDisable(payload)
      ElMessage.success('已解除绑定')
    } else {
      const res = await mfaRegenerateCodes(payload)
      newCodes.list = res.data.recovery_codes
      newCodes.visible = true
    }
    sensitive.visible = false
    await fetchStatus()
    if (status.value.enforced && !status.value.enabled) {
      authStore.setEnrollmentRequired(true)
    }
  } finally {
    submitting.value = false
  }
}

function copyCodes() {
  copyText(enroll.recoveryCodes.join('\n'), '恢复码已复制')
}

function downloadCodes() {
  const text = [
    `${authStore.username} 的 MFA 恢复码`,
    '每个恢复码只能使用一次，请妥善保管。',
    '',
    ...enroll.recoveryCodes,
  ].join('\n')
  const url = URL.createObjectURL(new Blob([text], { type: 'text/plain;charset=utf-8' }))
  const link = document.createElement('a')
  link.href = url
  link.download = `mfa-recovery-codes-${authStore.username}.txt`
  link.click()
  URL.revokeObjectURL(url)
}

async function copyText(text, msg) {
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success(msg)
  } catch {
    // 非 HTTPS 或浏览器不允许时退回手动复制
    ElMessage.warning('浏览器不允许自动复制，请手动选中复制')
  }
}

function formatTime(value) {
  return value ? new Date(value).toLocaleString('zh-CN') : ''
}
</script>

<style scoped>
.card-header {
  display: flex;
  align-items: center;
  gap: 8px;
}
.intro {
  color: #606266;
  font-size: 14px;
  line-height: 1.7;
  margin: 0 0 16px;
}
.actions {
  margin-top: 16px;
  display: flex;
  gap: 8px;
}
.enroll-body {
  display: flex;
  gap: 28px;
  flex-wrap: wrap;
}
.qr-box {
  text-align: center;
}
.manual {
  flex: 1;
  min-width: 280px;
}
.manual p {
  margin: 0 0 8px;
  color: #606266;
  font-size: 14px;
}
.tip {
  color: #909399;
  font-size: 12px;
  margin-left: 6px;
}
.codes {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 8px;
}
.code {
  font-family: monospace;
  font-size: 14px;
  letter-spacing: 1px;
  background: #f5f7fa;
  border: 1px solid #e4e7ed;
  border-radius: 4px;
  padding: 6px 10px;
  text-align: center;
}
</style>
