<template>
  <div class="login-page">
    <el-card class="login-card">
      <div class="callback-body">
        <template v-if="error">
          <el-icon size="40" color="#F56C6C"><CircleClose /></el-icon>
          <h3>飞书登录失败</h3>
          <p class="reason">{{ error }}</p>
          <el-button type="primary" @click="backToLogin">返回登录页</el-button>
        </template>
        <template v-else>
          <el-icon size="40" color="#409EFF" class="spinning"><Loading /></el-icon>
          <h3>正在完成飞书登录…</h3>
          <p class="reason">请稍候，正在与运维平台核对身份</p>
        </template>
      </div>
    </el-card>
  </div>
</template>

<script setup>
/**
 * 飞书授权回调落地页（公开路由 /login/feishu）。
 *
 * 飞书把用户浏览器带回这里，URL 上带 code 与 state；本页把两者交给后端换取
 * 平台令牌。**飞书服务器不会访问本页**——授权码是随浏览器回来的，所以这条路由
 * 不需要像 /api/feishu/callback/ 那两条入站入口那样加固。
 *
 * 换完令牌立刻用 replace 跳走：把带 code 的 URL 留在历史记录里，用户一回退就会
 * 拿一个已作废的 code 再试一次，然后看到一个没法自行解释的失败。
 */
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { CircleClose, Loading } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '../stores/auth'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()
const error = ref('')

onMounted(async () => {
  const { code, state, error: authError } = route.query
  if (authError) {
    error.value = `飞书返回错误：${authError}`
    return
  }
  if (!code || !state) {
    error.value = '授权信息不完整，请重新从登录页发起飞书登录。'
    return
  }
  try {
    await authStore.loginWithFeishu(String(code), String(state))
    ElMessage.success('登录成功')
    router.replace('/')
  } catch (err) {
    // 后端把失败原因写成了可行动的中文（未绑定、账号停用、state 失效等），
    // 直接展示，不要盖成「登录失败」
    error.value = err?.response?.data?.msg || '飞书登录失败，请重试或改用账号密码登录。'
  }
})

function backToLogin() {
  router.replace('/login')
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
.callback-body {
  text-align: center;
  padding: 24px 8px 12px;
}
.callback-body h3 {
  margin: 12px 0 6px;
  font-size: 18px;
  color: #303133;
}
.reason {
  margin: 0 0 18px;
  font-size: 13px;
  color: #909399;
  line-height: 1.6;
}
.spinning {
  animation: rotate 1.2s linear infinite;
}
@keyframes rotate {
  to { transform: rotate(360deg); }
}
</style>
