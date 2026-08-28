<template>
  <el-container class="app-layout">
    <!-- 侧边栏 -->
    <el-aside :width="collapsed ? '64px' : '220px'" class="sidebar">
      <div class="logo" :class="{ collapsed }">
        <el-icon size="24" color="#fff"><Monitor /></el-icon>
        <span v-if="!collapsed" class="logo-text">OpsManager</span>
      </div>
      <el-menu
        :default-active="route.meta.activeMenu || route.path"
        :collapse="collapsed"
        unique-opened
        router
        background-color="#001529"
        text-color="#a6adb4"
        active-text-color="#fff"
        class="sidebar-menu"
      >
        <el-menu-item index="/dashboard">
          <el-icon><DataBoard /></el-icon>
          <template #title>概览</template>
        </el-menu-item>
        <!-- 菜单可见性按角色矩阵判定（authStore.can，与后端 roles.MODULES 同名）。
             整组都无权限时连父菜单一起藏：留一个点进去全是 403 的空目录没有意义。
             这只是「少让人点到必然 403 的入口」，真正的判定始终在后端 -->
        <el-sub-menu v-if="authStore.can('cmdb')" index="assets">
          <template #title>
            <el-icon><Box /></el-icon>
            <span>资产管理</span>
          </template>
          <el-menu-item index="/assets/servers">服务器</el-menu-item>
        </el-sub-menu>
        <el-sub-menu v-if="canCloud" index="cloud">
          <template #title>
            <el-icon><Cloudy /></el-icon>
            <span>云控管理</span>
          </template>
          <el-menu-item v-if="authStore.can('cloud_account', 'write')" index="/cloud/accounts">云账号</el-menu-item>
          <el-menu-item v-if="authStore.can('cloud')" index="/cloud/domains">域名管理</el-menu-item>
          <el-menu-item v-if="authStore.can('cloud')" index="/cloud/certificates">证书管理</el-menu-item>
        </el-sub-menu>
        <el-sub-menu v-if="canConfig" index="config">
          <template #title>
            <el-icon><SetUp /></el-icon>
            <span>配置管理</span>
          </template>
          <template v-if="authStore.can('dict')">
            <el-menu-item index="/config/project-groups">项目组</el-menu-item>
            <el-menu-item index="/config/channels">渠道</el-menu-item>
            <el-menu-item index="/config/cloud-providers">云厂商</el-menu-item>
            <el-menu-item index="/config/environments">环境</el-menu-item>
            <el-menu-item index="/config/service-types">服务类型</el-menu-item>
          </template>
          <el-menu-item v-if="authStore.can('cmdb')" index="/config/allocation">服务器分配</el-menu-item>
        </el-sub-menu>
        <el-sub-menu v-if="canTask" index="task">
          <template #title>
            <el-icon><Promotion /></el-icon>
            <span>任务执行</span>
          </template>
          <el-menu-item v-if="authStore.can('method', 'write')" index="/task/methods">方法管理</el-menu-item>
          <el-menu-item v-if="authStore.can('variable')" index="/task/variables">变量管理</el-menu-item>
          <el-menu-item v-if="authStore.can('exec')" index="/task/executions">任务列表</el-menu-item>
          <el-menu-item v-if="authStore.can('exec', 'write')" index="/task/log-files">日志文件</el-menu-item>
          <el-menu-item v-if="authStore.can('artifact')" index="/task/artifacts">制品管理</el-menu-item>
          <el-menu-item v-if="authStore.can('credential')" index="/task/credentials">连接凭据</el-menu-item>
        </el-sub-menu>
        <el-menu-item v-if="authStore.can('audit')" index="/audit/logs">
          <el-icon><Document /></el-icon>
          <template #title>操作审计</template>
        </el-menu-item>
        <el-sub-menu v-if="canSystem" index="system">
          <template #title>
            <el-icon><Setting /></el-icon>
            <span>系统管理</span>
          </template>
          <el-menu-item v-if="authStore.can('account', 'write')" index="/system/users">用户管理</el-menu-item>
          <el-menu-item v-if="authStore.can('account', 'write')" index="/system/roles">角色权限</el-menu-item>
          <el-menu-item v-if="authStore.can('agent')" index="/system/agents">Agent 管理</el-menu-item>
          <el-menu-item v-if="authStore.can('account', 'write')" index="/system/api-credentials">API 凭证</el-menu-item>
          <el-menu-item v-if="authStore.can('system', 'write')" index="/system/configs">系统配置</el-menu-item>
        </el-sub-menu>
      </el-menu>
    </el-aside>

    <el-container>
      <!-- 顶部导航 -->
      <el-header class="header">
        <el-icon
          class="collapse-btn"
          size="20"
          @click="collapsed = !collapsed"
        >
          <Fold v-if="!collapsed" /><Expand v-else />
        </el-icon>
        <el-breadcrumb separator="/">
          <el-breadcrumb-item :to="{ path: '/' }">首页</el-breadcrumb-item>
          <el-breadcrumb-item v-if="route.meta.title">
            {{ route.meta.title }}
          </el-breadcrumb-item>
        </el-breadcrumb>
        <div class="header-right">
          <!-- sudo 窗口提示：解释「为什么这次改配置没弹验证框」 -->
          <el-tooltip
            v-if="sudoLeft > 0"
            content="已通过二次验证，窗口内修改凭据/用户/Agent/云账号/系统配置免重验"
            placement="bottom"
          >
            <el-tag type="success" size="small" effect="plain" style="margin-right:12px">
              <el-icon><Unlock /></el-icon>
              二次验证 {{ Math.ceil(sudoLeft / 60) }} 分钟
            </el-tag>
          </el-tooltip>
          <el-dropdown @command="handleCommand">
            <span class="user-info">
              <el-avatar size="small" :style="{ background: '#409EFF' }">
                {{ authStore.username?.[0]?.toUpperCase() }}
              </el-avatar>
              <span class="username">{{ authStore.username }}</span>
              <el-icon><ArrowDown /></el-icon>
            </span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="password">
                  <el-icon><Lock /></el-icon> 修改密码
                </el-dropdown-item>
                <el-dropdown-item command="mfa">
                  <el-icon><Key /></el-icon> 二次认证
                </el-dropdown-item>
                <el-dropdown-item command="logout" divided>
                  <el-icon><SwitchButton /></el-icon> 退出登录
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </el-header>

      <!-- 主内容区 -->
      <el-main class="main-content">
        <router-view />
      </el-main>
    </el-container>
  </el-container>

  <!-- 修改密码弹窗 -->
  <el-dialog v-model="pwdVisible" title="修改密码" width="420px">
    <el-form ref="pwdFormRef" :model="pwdForm" :rules="pwdRules" label-width="90px">
      <el-form-item label="原密码" prop="old_password">
        <el-input v-model="pwdForm.old_password" type="password" show-password />
      </el-form-item>
      <el-form-item label="新密码" prop="new_password">
        <el-input v-model="pwdForm.new_password" type="password" show-password />
      </el-form-item>
      <el-form-item label="确认密码" prop="confirm">
        <el-input v-model="pwdForm.confirm" type="password" show-password />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="pwdVisible = false">取消</el-button>
      <el-button type="primary" :loading="pwdSubmitting" @click="submitPassword">确定</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  Monitor, DataBoard, Box, Document, Setting, SetUp,
  Fold, Expand, ArrowDown, SwitchButton, Lock, Promotion, Cloudy, Key, Unlock,
} from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useAuthStore } from '../stores/auth'
import { changePassword } from '../api/auth'
import { sudoRemaining, clearSudo } from '../utils/sudo'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()
const collapsed = ref(false)

// 分组菜单的可见性：组内一个子项都看不到时把父菜单也藏掉
const canCloud = computed(() =>
  authStore.can('cloud') || authStore.can('cloud_account', 'write'))
const canConfig = computed(() => authStore.can('dict') || authStore.can('cmdb'))
const canTask = computed(() => ['exec', 'variable', 'artifact', 'credential']
  .some((module) => authStore.can(module)) || authStore.can('method', 'write'))
const canSystem = computed(() =>
  authStore.can('account', 'write') || authStore.can('agent')
  || authStore.can('system', 'write'))

// sudo 窗口剩余秒数：每 10s 刷一次，到点标签自动消失
const sudoLeft = ref(sudoRemaining())
let sudoTimer = null
onMounted(() => {
  sudoTimer = setInterval(() => { sudoLeft.value = sudoRemaining() }, 10000)
  // 每次进主框架刷一次权限快照：快照原来只在登录时取，后端改了角色矩阵
  // （拆模块、调档位）之后，没重新登录的会话会一直拿着旧快照，菜单要么少一块、
  // 要么多一块。失败不打扰用户——沿用旧快照，真正的判定在后端
  authStore.fetchMe().catch(() => {})
})
onUnmounted(() => clearInterval(sudoTimer))

const pwdVisible = ref(false)
const pwdSubmitting = ref(false)
const pwdFormRef = ref()
const pwdForm = reactive({ old_password: '', new_password: '', confirm: '' })
const pwdRules = {
  old_password: [{ required: true, message: '请输入原密码' }],
  new_password: [{ required: true, message: '请输入新密码' }, { min: 8, message: '至少 8 个字符' }],
  confirm: [
    { required: true, message: '请再次输入新密码' },
    {
      validator: (_r, v, cb) =>
        v === pwdForm.new_password ? cb() : cb(new Error('两次输入不一致')),
    },
  ],
}

async function handleCommand(cmd) {
  if (cmd === 'logout') {
    await ElMessageBox.confirm('确定要退出登录吗？', '提示', { type: 'warning' })
    await authStore.logout()
    clearSudo()
    sudoLeft.value = 0
    router.push('/login')
  } else if (cmd === 'password') {
    Object.assign(pwdForm, { old_password: '', new_password: '', confirm: '' })
    pwdVisible.value = true
    pwdFormRef.value?.clearValidate()
  } else if (cmd === 'mfa') {
    router.push({ name: 'SecurityMfa' })
  }
}

async function submitPassword() {
  await pwdFormRef.value.validate()
  pwdSubmitting.value = true
  try {
    await changePassword({ old_password: pwdForm.old_password, new_password: pwdForm.new_password })
    clearSudo()   // 后端已 revoke 窗口，本地提示同步清掉
    sudoLeft.value = 0
    ElMessage.success('密码修改成功，请重新登录')
    pwdVisible.value = false
    await authStore.logout()
    clearSudo()
    sudoLeft.value = 0
    router.push('/login')
  } finally {
    pwdSubmitting.value = false
  }
}
</script>

<style scoped>
.app-layout { height: 100vh; }
.sidebar {
  background: #001529;
  transition: width 0.3s;
  /* 纵向 flex：logo 固定，菜单区自身滚动（原 overflow:hidden 会裁掉超长菜单） */
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.logo {
  height: 60px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  border-bottom: 1px solid #002040;
}
.logo-text { color: #fff; font-size: 16px; font-weight: 600; white-space: nowrap; }
.sidebar-menu {
  border-right: none;
  flex: 1;
  overflow-y: auto;
  /* 折叠态弹出子菜单靠 popper 渲染在 body，不受此处滚动影响 */
  overflow-x: hidden;
}
/* 暗色细滚动条：默认滚动条在深色侧栏上很突兀 */
.sidebar-menu::-webkit-scrollbar { width: 6px; }
.sidebar-menu::-webkit-scrollbar-thumb {
  background: rgba(255, 255, 255, 0.18);
  border-radius: 3px;
}
.sidebar-menu::-webkit-scrollbar-thumb:hover { background: rgba(255, 255, 255, 0.3); }
.sidebar-menu::-webkit-scrollbar-track { background: transparent; }
.header {
  display: flex;
  align-items: center;
  gap: 16px;
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
  padding: 0 20px;
}
.collapse-btn { cursor: pointer; color: #606266; flex-shrink: 0; }
.header-right { margin-left: auto; }
.user-info {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  color: #606266;
}
.username { font-size: 14px; }
.main-content { background: #f0f2f5; padding: 20px; overflow-y: auto; }
</style>
