import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'Login',
      component: () => import('../views/Login.vue'),
      meta: { public: true },
    },
    {
      path: '/',
      component: () => import('../views/Layout.vue'),
      redirect: '/dashboard',
      children: [
        {
          path: 'dashboard',
          name: 'Dashboard',
          component: () => import('../views/Dashboard.vue'),
          meta: { title: '概览' },
        },
        {
          path: 'assets/servers',
          name: 'Servers',
          component: () => import('../views/assets/Servers.vue'),
          meta: { title: '服务器管理' },
        },
        // 云控管理：云账号（仅管理员）+ 域名管理（主域名/解析记录）
        {
          path: 'cloud/accounts',
          name: 'CloudAccounts',
          component: () => import('../views/cloud/CloudAccounts.vue'),
          meta: { title: '云账号', staff: true },
        },
        {
          path: 'cloud/domains',
          name: 'CloudDomains',
          component: () => import('../views/cloud/Domains.vue'),
          meta: { title: '域名管理' },
        },
        {
          path: 'cloud/certificates',
          name: 'CloudCertificates',
          component: () => import('../views/cloud/Certificates.vue'),
          meta: { title: '证书管理' },
        },
        // 配置管理：服务器分配（归属分配 + 服务规划，仅显示已规划主机）+ CMDB 基础数据字典
        {
          path: 'config/allocation',
          name: 'ConfigAllocation',
          component: () => import('../views/config/Allocation.vue'),
          meta: { title: '服务器分配' },
        },
        {
          path: 'config/project-groups',
          name: 'ProjectGroups',
          component: () => import('../views/config/DictManager.vue'),
          props: { type: 'project-groups' },
          meta: { title: '项目组' },
        },
        {
          path: 'config/channels',
          name: 'Channels',
          component: () => import('../views/config/DictManager.vue'),
          props: { type: 'channels' },
          meta: { title: '渠道' },
        },
        {
          path: 'config/environments',
          name: 'Environments',
          component: () => import('../views/config/DictManager.vue'),
          props: { type: 'environments' },
          meta: { title: '环境' },
        },
        {
          path: 'config/cloud-providers',
          name: 'CloudProviders',
          component: () => import('../views/config/DictManager.vue'),
          props: { type: 'cloud-providers' },
          meta: { title: '云厂商' },
        },
        {
          path: 'config/service-types',
          name: 'ServiceTypes',
          component: () => import('../views/config/DictManager.vue'),
          props: { type: 'service-types' },
          meta: { title: '服务类型' },
        },
        {
          path: 'task/methods',
          name: 'TaskMethods',
          component: () => import('../views/task/Methods.vue'),
          meta: { title: '方法管理', staff: true },
        },
        {
          path: 'task/variables',
          name: 'TaskVariables',
          component: () => import('../views/task/Variables.vue'),
          meta: { title: '变量管理' },
        },
        {
          path: 'task/executions',
          name: 'TaskExecutions',
          component: () => import('../views/task/Executions.vue'),
          meta: { title: '任务执行' },
        },
        // 独立执行日志页：URL 可分享定位到某任务的某次执行（logId 可省略=最新一次）
        {
          path: 'task/executions/:taskId(\\d+)/logs/:logId(\\d+)?',
          name: 'TaskLog',
          component: () => import('../views/task/TaskLog.vue'),
          meta: { title: '执行日志', activeMenu: '/task/executions' },
        },
        {
          path: 'task/log-files',
          name: 'TaskLogFiles',
          component: () => import('../views/task/LogFiles.vue'),
          meta: { title: '日志文件', staff: true },
        },
        {
          path: 'task/artifacts',
          name: 'TaskArtifacts',
          component: () => import('../views/task/Artifacts.vue'),
          meta: { title: '制品管理' },
        },
        {
          path: 'task/credentials',
          name: 'TaskCredentials',
          component: () => import('../views/task/Credentials.vue'),
          meta: { title: '连接凭据', staff: true },
        },
        {
          path: 'audit/logs',
          name: 'AuditLogs',
          component: () => import('../views/audit/Logs.vue'),
          meta: { title: '操作审计' },
        },
        {
          path: 'system/users',
          name: 'Users',
          component: () => import('../views/system/Users.vue'),
          meta: { title: '用户管理', staff: true },
        },
        {
          path: 'system/agents',
          name: 'SystemAgents',
          component: () => import('../views/system/Agents.vue'),
          meta: { title: 'Agent 管理', staff: true },
        },
        {
          path: 'system/configs',
          name: 'SystemConfigs',
          component: () => import('../views/system/Configs.vue'),
          meta: { title: '系统配置' },
        },
        // 二次认证（所有人可见，入口在右上角头像菜单，不进侧边栏）
        {
          path: 'security/mfa',
          name: 'SecurityMfa',
          component: () => import('../views/SecurityMfa.vue'),
          meta: { title: '二次认证' },
        },
      ],
    },
  ],
})

// 路由守卫：未登录跳转登录页；非管理员禁止访问 staff 页面
router.beforeEach((to) => {
  const token = localStorage.getItem('access_token')
  if (!to.meta.public && !token) {
    return { name: 'Login' }
  }
  if (to.name === 'Login' && token) {
    return { name: 'Dashboard' }
  }
  if (to.meta.staff) {
    const user = JSON.parse(localStorage.getItem('user') || 'null')
    if (!user?.is_staff) {
      return { name: 'Dashboard' }
    }
  }
  // 强制 MFA 但未绑定：除绑定页本身外一律拦到绑定页（后端也会拦，这里只是
  // 少一次失败请求）
  if (token && localStorage.getItem('mfa_enrollment') === '1'
      && to.name !== 'SecurityMfa') {
    return { name: 'SecurityMfa' }
  }
})

export default router
