# 路由与页面

**文件：** `src/router/index.js`

---

## 路由表

| 路径 | 组件 | 是否公开 | 说明 |
|------|------|----------|------|
| `/login` | `views/Login.vue` | ✅ 公开 | 登录页 |
| `/` | `views/Layout.vue` | 需认证 | 布局容器，重定向 → `/dashboard` |
| `/dashboard` | `views/Dashboard.vue` | 需认证 | 概览仪表板 |
| `/assets/servers` | `views/assets/Servers.vue` | 需认证 | 服务器管理 |
| `/audit/logs` | `views/audit/Logs.vue` | 需认证 | 操作审计日志 |
| `/system/api-credentials` | `views/system/ApiCredentials.vue` | 需认证（仅管理员）| API 凭证（机读接口访问控制）|
| `/system/configs` | `views/system/Configs.vue` | 需认证 | 系统配置 |

---

## 导航守卫

```javascript
router.beforeEach((to) => {
  const token = localStorage.getItem('access_token')

  // 未登录访问受保护页面 → 跳转登录
  if (!to.meta.public && !token) {
    return { name: 'Login' }
  }

  // 已登录访问登录页 → 跳转首页
  if (to.name === 'Login' && token) {
    return { name: 'Dashboard' }
  }
})
```

---

## 页面 meta 配置

| 字段 | 类型 | 说明 |
|------|------|------|
| `public` | boolean | `true` 表示无需认证，默认需要认证 |
| `title` | string | 面包屑标题（Layout.vue 中读取）|
| `perm` | `[模块, 动作]` | 页面权限，按角色矩阵判定（如 `['cloud_account', 'write']`）。动作取 `read` / `create` / `write`。权限快照缺失时回退 `is_staff` |

`perm` 取代了原来的 `staff: true`：`is_staff` 只有两档，表达不出「运维管理能改云账号
但不是超级管理」。判定口径与 `stores/auth.js` 的 `can()` 一致，**只是少一次必然 403 的
跳转**，真正的判定始终在后端。

---

## 路由历史模式

使用 `createWebHistory()`（HTML5 History 模式），URL 无 `#` 号。

> 生产部署时需配置 Web 服务器（Nginx）将所有路由回退到 `index.html`：
> ```nginx
> location / {
>     try_files $uri $uri/ /index.html;
> }
> ```
