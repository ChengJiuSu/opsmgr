# 主布局（Layout.vue）

**路由：** `/`（所有需认证页面的父路由）  
**文件：** `src/views/Layout.vue`

---

## 布局结构

```
el-container（整页高度）
├── el-aside（侧边栏，220px / 64px 折叠）
│   ├── Logo（图标 + 文字，折叠时只显示图标）
│   └── el-menu（导航菜单，router 模式）
│       ├── 概览         → /dashboard
│       ├── 资产管理（子菜单）
│       │   └── 服务器  → /assets/servers
│       ├── 操作审计     → /audit/logs
│       └── 系统配置     → /system/configs
└── el-container（右侧主区域）
    ├── el-header（顶部栏，60px）
    │   ├── 折叠按钮（Fold / Expand 图标）
    │   ├── el-breadcrumb（面包屑导航，读取 route.meta.title）
    │   └── 用户下拉菜单（头像首字母 + 用户名 + 退出登录）
    └── el-main（内容区，灰色背景 #f0f2f5）
        └── router-view（子页面渲染区）
```

---

## 侧边栏折叠

```javascript
const collapsed = ref(false)
// 宽度：collapsed ? '64px' : '220px'
// 菜单：:collapse="collapsed"
```

---

## 菜单展开与滚动

分组多了会超出视口，两条措施配合：

- **手风琴**：`el-menu` 加 `unique-opened`，展开新分组自动收起旧分组，
  同时只有一组展开。当前路由所属分组由 Element Plus `initMenu` 自动展开
  （挂载时按 activeIndex 的 indexPath 逐级 open），无需手动传 `default-openeds`。
- **可滚动**：`.sidebar` 改纵向 flex（logo `flex-shrink: 0` 固定），
  `.sidebar-menu` `flex: 1 + overflow-y: auto` 自身滚动——原先 `.sidebar`
  只有 `overflow: hidden`，菜单超长会被直接裁掉。深色侧栏配 6px 半透明
  细滚动条（`::-webkit-scrollbar`）。

---

## 退出登录

```javascript
async function handleCommand(cmd) {
  if (cmd === 'logout') {
    await ElMessageBox.confirm('确定要退出登录吗？', '提示', { type: 'warning' })
    authStore.logout()
    router.push('/login')
  }
}
```

---

## 样式要点

| 区域 | 样式 |
|------|------|
| 侧边栏背景 | `#001529`（深海军蓝）|
| 菜单文字 | `#a6adb4`，激活为 `#fff` |
| 顶部栏背景 | `#fff`，底部边框 `#e4e7ed` |
| 内容区背景 | `#f0f2f5` |
| 过渡动画 | `transition: width 0.3s` |
