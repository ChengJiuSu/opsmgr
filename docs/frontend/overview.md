# 前端架构总览

## 技术栈

| 组件 | 版本 | 说明 |
|------|------|------|
| Vue | 3.5 | 渐进式前端框架 |
| Vite | 8.0 | 构建工具 |
| Element Plus | 2.14 | UI 组件库 |
| Vue Router | 4.6 | 路由管理 |
| Pinia | 3.0 | 状态管理 |
| Axios | 1.17 | HTTP 客户端 |

---

## 目录结构

```
frontend/
├── index.html
├── vite.config.js              # Vite 配置（代理、端口）
├── package.json
└── src/
    ├── main.js                 # 应用入口
    ├── App.vue                 # 根组件
    ├── style.css               # 全局样式
    ├── router/
    │   └── index.js            # 路由配置 & 导航守卫
    ├── stores/
    │   └── auth.js             # Pinia 认证状态
    ├── utils/
    │   └── request.js          # Axios 封装（拦截器）
    ├── api/
    │   ├── auth.js             # 认证接口
    │   ├── assets.js           # 资产接口
    │   ├── audit.js            # 审计接口
    │   └── system.js           # 系统配置接口
    └── views/
        ├── Login.vue           # 登录页
        ├── Layout.vue          # 主布局（侧边栏 + 顶栏）
        ├── Dashboard.vue       # 概览仪表板
        ├── assets/
        │   └── Servers.vue     # 服务器管理
        ├── audit/
        │   └── Logs.vue        # 审计日志
        └── system/
            └── Configs.vue     # 系统配置
```

---

## 应用初始化流程

```
main.js
  ├── createApp(App)
  ├── 注册所有 Element Plus 图标
  ├── use(createPinia())          → 状态管理
  ├── use(router)                 → 路由
  ├── use(ElementPlus, zhCn)      → UI + 中文
  └── mount('#app')
```

---

## 数据流向

```
用户操作
    │
    ▼
View (.vue)
    │  调用
    ▼
api/*.js                         → 封装的接口函数
    │  通过
    ▼
utils/request.js (Axios 实例)
    │  请求拦截器：附加 Authorization: Bearer <token>
    │  响应拦截器：401 → 跳转登录；错误 → ElMessage 提示
    ▼
Django 后端 /api/...
    │
    ▼
响应数据 → View 渲染
```

---

## 路由结构

```
/login                           公开，登录页
/                                需认证，重定向 → /dashboard
  ├── /dashboard                 概览
  ├── /assets/servers            服务器管理
  ├── /audit/logs                操作审计
  └── /system/configs            系统配置
```

导航守卫：未携带 `access_token` 的请求一律跳转 `/login`；已登录时访问 `/login` 自动跳转 `/dashboard`。

---

## 开发服务器

```bash
cd frontend
npm install
npm run dev          # 启动在 http://localhost:5173
```

Vite 代理配置（开发环境，避免跨域）：

```
/api  →  http://localhost:8000
```

生产构建：

```bash
npm run build        # 输出到 dist/
```

---

## 相关文档

- [路由与页面](router.md)
- [状态管理](state.md)
- [API 调用层](api.md)
- [HTTP 请求封装](request.md)
- [登录页](views/login.md)
- [主布局](views/layout.md)
- [仪表板](views/dashboard.md)
- [服务器管理](views/servers.md)
- [审计日志](views/audit-logs.md)
- [系统配置](views/system-configs.md)
