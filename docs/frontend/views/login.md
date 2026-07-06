# 登录页（Login.vue）

**路由：** `/login`  
**文件：** `src/views/Login.vue`

---

## 功能

- 用户名 + 密码登录，成功后跳转 `/dashboard`
- 支持 Enter 键快捷提交
- 表单校验（用户名、密码均为必填）
- 登录按钮展示 loading 状态防重复提交

---

## 组件结构

```
登录页
└── el-card（卡片容器）
    ├── header：Logo + 系统名称
    └── el-form
        ├── el-form-item：用户名输入框（User 图标）
        ├── el-form-item：密码输入框（Lock 图标，支持显隐）
        └── el-button：登录按钮（loading 状态）
```

---

## 核心逻辑

```javascript
async function handleLogin() {
  await formRef.value.validate()        // 表单校验
  loading.value = true
  try {
    await authStore.login(form)         // 调用 Pinia action
    ElMessage.success('登录成功')
    router.push('/')
  } finally {
    loading.value = false
  }
}
```

---

## 样式

- 背景：深蓝色渐变 `#1a1a2e → #16213e → #0f3460`
- 卡片：居中显示，宽 400px，圆角 12px，深阴影
