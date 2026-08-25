# 登录页（Login.vue）

**路由：** `/login`  
**文件：** `src/views/Login.vue`

---

## 功能

- 用户名 + 密码登录，成功后跳转 `/dashboard`
- **已绑定 MFA 的账号进第二步**：输 6 位验证码，或切换为恢复码
- 支持 Enter 键快捷提交
- 表单校验（用户名、密码均为必填）
- 登录按钮展示 loading 状态防重复提交

---

## 组件结构

```
登录页
└── el-card（卡片容器）
    ├── header：Logo + 系统名称
    ├── el-form（step='password'）
    │   ├── el-form-item：用户名输入框（User 图标）
    │   ├── el-form-item：密码输入框（Lock 图标，支持显隐）
    │   └── el-button：登录按钮（loading 状态）
    └── el-form（step='mfa'）
        ├── el-input：验证码 / 恢复码（Key 图标）
        ├── el-button：验证
        └── el-link：切换恢复码 / 返回
```

---

## 核心逻辑

```javascript
const res = await authStore.login(form)
if (res.mfaRequired) {          // 已绑定 MFA：这一步拿不到访问令牌
  step.value = 'mfa'            // 挑战票据留在 store 内存里（不落 localStorage）
  return
}
afterLogin()                    // 强制未绑定则跳绑定页，否则进首页
```

第二步：

```javascript
const res = await authStore.verifyMfa(code)     // 票据 + 验证码 → 正式令牌
if (res.recoveryCodesLeft !== undefined) {      // 用的是恢复码，提示剩余数量
  ElMessage.warning(`已使用一个恢复码，剩余 ${res.recoveryCodesLeft} 个`)
}
```

细节：

- 验证码输入框只留数字，满 6 位**自动提交**（省一次点击，错了立刻能重来）；
- 第二步返回 401（票据过期或错误次数用尽）时自动退回密码步；
- 挑战票据只放 Pinia 内存：它等价于半个凭据，且 5 分钟就过期，刷新页面重新
  输密码即可。

---

## 样式

- 背景：深蓝色渐变 `#1a1a2e → #16213e → #0f3460`
- 卡片：居中显示，宽 400px，圆角 12px，深阴影

---

## MFA 相关

挑战票据、`mfa_enrollment_required` 标记与路由守卫的配合见
`docs/frontend/views/security-mfa.md`；后端协议见
`docs/backend/modules/auth.md` 的 MFA 章节。
