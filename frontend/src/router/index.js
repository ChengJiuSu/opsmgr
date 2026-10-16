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
      // 飞书授权回调落地页：飞书把浏览器带回这里，带 code 与 state。
      // 地址要与飞书后台「重定向 URL」白名单、以及 FeishuApp.sso_redirect_uri 一致
      path: '/login/feishu',
      name: 'LoginFeishu',
      component: () => import('../views/LoginFeishu.vue'),
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
          meta: { title: '服务器管理', perm: ['cmdb', 'read'] },
        },
        // 云控管理：云账号（仅管理员）+ 域名管理（主域名/解析记录）
        // 拓扑画布：列表 + 编辑器（编辑器路由带 id，画布本身可分享定位）
        {
          path: 'topo/canvases',
          name: 'TopoCanvases',
          component: () => import('../views/topo/Canvases.vue'),
          meta: { title: '拓扑画布', perm: ['topo', 'read'] },
        },
        {
          path: 'topo/canvases/:id(\\d+)',
          name: 'TopoCanvasEditor',
          component: () => import('../views/topo/CanvasEditor.vue'),
          meta: { title: '画布编辑', activeMenu: '/topo/canvases', perm: ['topo', 'read'] },
        },
        {
          path: 'cloud/accounts',
          name: 'CloudAccounts',
          component: () => import('../views/cloud/CloudAccounts.vue'),
          meta: { title: '云账号', perm: ['cloud_account', 'write'] },
        },
        {
          path: 'cloud/domains',
          name: 'CloudDomains',
          component: () => import('../views/cloud/Domains.vue'),
          meta: { title: '域名管理', perm: ['cloud', 'read'] },
        },
        {
          path: 'cloud/certificates',
          name: 'CloudCertificates',
          component: () => import('../views/cloud/Certificates.vue'),
          meta: { title: '证书管理', perm: ['cloud', 'read'] },
        },
        // 配置管理：服务器分配（归属分配 + 服务规划，仅显示已规划主机）+ CMDB 基础数据字典
        {
          path: 'config/allocation',
          name: 'ConfigAllocation',
          component: () => import('../views/config/Allocation.vue'),
          meta: { title: '服务器分配', perm: ['cmdb', 'read'] },
        },
        {
          path: 'config/project-groups',
          name: 'ProjectGroups',
          component: () => import('../views/config/DictManager.vue'),
          props: { type: 'project-groups' },
          meta: { title: '项目组', perm: ['dict', 'read'] },
        },
        {
          path: 'config/channels',
          name: 'Channels',
          component: () => import('../views/config/DictManager.vue'),
          props: { type: 'channels' },
          meta: { title: '渠道', perm: ['dict', 'read'] },
        },
        {
          path: 'config/environments',
          name: 'Environments',
          component: () => import('../views/config/DictManager.vue'),
          props: { type: 'environments' },
          meta: { title: '环境', perm: ['dict', 'read'] },
        },
        {
          path: 'config/cloud-providers',
          name: 'CloudProviders',
          component: () => import('../views/config/DictManager.vue'),
          props: { type: 'cloud-providers' },
          meta: { title: '云厂商', perm: ['dict', 'read'] },
        },
        {
          path: 'config/service-types',
          name: 'ServiceTypes',
          component: () => import('../views/config/DictManager.vue'),
          props: { type: 'service-types' },
          meta: { title: '服务类型', perm: ['dict', 'read'] },
        },
        {
          path: 'config/portal',
          name: 'ConfigPortal',
          component: () => import('../views/config/Portal.vue'),
          meta: { title: '内部导航页', perm: ['dict', 'read'] },
        },
        {
          path: 'task/methods',
          name: 'TaskMethods',
          component: () => import('../views/task/Methods.vue'),
          meta: { title: '方法管理', perm: ['method', 'write'] },
        },
        {
          path: 'task/variables',
          name: 'TaskVariables',
          component: () => import('../views/task/Variables.vue'),
          meta: { title: '变量管理', perm: ['variable', 'read'] },
        },
        {
          path: 'task/executions',
          name: 'TaskExecutions',
          component: () => import('../views/task/Executions.vue'),
          meta: { title: '任务执行', perm: ['exec', 'read'] },
        },
        // 独立执行日志页：URL 可分享定位到某任务的某次执行（logId 可省略=最新一次）
        {
          path: 'task/executions/:taskId(\\d+)/logs/:logId(\\d+)?',
          name: 'TaskLog',
          component: () => import('../views/task/TaskLog.vue'),
          meta: { title: '执行日志', activeMenu: '/task/executions', perm: ['exec', 'read'] },
        },
        {
          path: 'task/log-files',
          name: 'TaskLogFiles',
          component: () => import('../views/task/LogFiles.vue'),
          meta: { title: '日志文件', perm: ['exec', 'read'] },
        },
        {
          path: 'task/artifacts',
          name: 'TaskArtifacts',
          component: () => import('../views/task/Artifacts.vue'),
          meta: { title: '制品管理', perm: ['artifact', 'read'] },
        },
        {
          path: 'task/credentials',
          name: 'TaskCredentials',
          component: () => import('../views/task/Credentials.vue'),
          meta: { title: '连接凭据', perm: ['credential', 'read'] },
        },
        {
          path: 'audit/logs',
          name: 'AuditLogs',
          component: () => import('../views/audit/Logs.vue'),
          meta: { title: '操作审计', perm: ['audit', 'read'] },
        },
        {
          path: 'system/users',
          name: 'Users',
          component: () => import('../views/system/Users.vue'),
          meta: { title: '用户管理', perm: ['account', 'write'] },
        },
        {
          path: 'system/agents',
          name: 'SystemAgents',
          component: () => import('../views/system/Agents.vue'),
          meta: { title: 'Agent 管理', perm: ['agent', 'read'] },
        },
        {
          path: 'system/roles',
          name: 'SystemRoles',
          component: () => import('../views/system/Roles.vue'),
          meta: { title: '角色权限', perm: ['account', 'write'] },
        },
        {
          path: 'system/api-credentials',
          name: 'SystemApiCredentials',
          component: () => import('../views/system/ApiCredentials.vue'),
          meta: { title: 'API 凭证', perm: ['account', 'write'] },
        },
        {
          path: 'system/configs',
          name: 'SystemConfigs',
          component: () => import('../views/system/Configs.vue'),
          meta: { title: '系统配置', perm: ['system', 'write'] },
        },
        {
          path: 'system/feishu',
          name: 'SystemFeishu',
          component: () => import('../views/system/Feishu.vue'),
          meta: { title: '飞书接入', perm: ['system', 'write'] },
        },
        {
          path: 'system/notify',
          name: 'SystemNotify',
          component: () => import('../views/system/Notify.vue'),
          meta: { title: '消息通知', perm: ['notify', 'read'] },
        },
        {
          // 绑定关系决定「哪个飞书号能以哪个平台账号进来」，属身份面 → account
          path: 'system/feishu-bindings',
          name: 'SystemFeishuBindings',
          component: () => import('../views/system/FeishuBindings.vue'),
          meta: { title: '飞书绑定', perm: ['account', 'write'] },
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

const LEVELS = ['none', 'read', 'read_create', 'write']
const ACTION_LEVEL = { read: 'read', create: 'read_create', write: 'write' }

/** 页面权限判定：与 stores/auth.js 的 can() 同一套口径。 */
function hasRoutePerm(user, perm) {
  const modules = user?.permissions?.modules
  if (!modules || !perm) return !!user?.is_staff
  // 快照里没有这个模块键 = 快照比该模块老（后端拆分模块前登录的会话），
  // 判为「不知道」回退 is_staff，否则老会话会被自己的页面挡在门外
  if (!(perm[0] in modules)) return !!user?.is_staff
  const level = modules[perm[0]] || 'none'
  return LEVELS.indexOf(level) >= LEVELS.indexOf(ACTION_LEVEL[perm[1]] || 'read')
}

// 路由守卫：未登录跳转登录页；页面权限按角色矩阵判定（meta.perm）
router.beforeEach((to) => {
  const token = localStorage.getItem('access_token')
  if (!to.meta.public && !token) {
    return { name: 'Login' }
  }
  if (to.name === 'Login' && token) {
    return { name: 'Dashboard' }
  }
  // 页面权限：meta.perm = [模块, 动作]，按角色权限快照判定。
  // 快照缺失（老 token / me 还没取到）时回退 is_staff，避免把有权限的人挡在门外。
  // 这里只是「少一次必然 403 的跳转」，真正的判定始终在后端。
  if (to.meta.perm || to.meta.staff) {
    const user = JSON.parse(localStorage.getItem('user') || 'null')
    if (!hasRoutePerm(user, to.meta.perm)) {
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
