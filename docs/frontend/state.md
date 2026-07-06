# 状态管理

**文件：** `src/stores/auth.js`  
**框架：** Pinia

---

## authStore

### 状态（State）

| 字段 | 类型 | 来源 | 说明 |
|------|------|------|------|
| `accessToken` | string | localStorage | JWT 访问令牌 |
| `refreshToken` | string | localStorage | JWT 刷新令牌 |
| `username` | string | localStorage | 当前登录用户名 |

### 计算属性（Getters）

| 字段 | 类型 | 说明 |
|------|------|------|
| `isLoggedIn` | boolean | `!!accessToken`，用于路由守卫和 UI 判断 |

### 方法（Actions）

#### `login(credentials)`

```javascript
await authStore.login({ username: 'admin', password: 'admin123' })
```

**流程：**
1. 调用 `POST /api/auth/login/`
2. 保存 `access` 和 `refresh` 到 state 和 localStorage
3. 解析 JWT Payload（`atob` + `JSON.parse`）提取 `username`
4. 保存 `username` 到 state 和 localStorage

#### `logout()`

```javascript
authStore.logout()
```

**流程：**
1. 清空 state 中的 token 和 username
2. 移除 localStorage 中的 `access_token`、`refresh_token`、`username`

---

## 持久化策略

状态通过 `localStorage` 手动持久化，页面刷新后自动恢复：

```javascript
// 初始化时从 localStorage 读取
const accessToken = ref(localStorage.getItem('access_token') || '')
```

> 当前未使用 `pinia-plugin-persistedstate`，如需自动持久化可后续引入。
