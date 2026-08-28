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

---

## 角色权限快照（本期新增）

`authStore.perms` 来自后端 `roles.permission_snapshot`（`/api/auth/me/` 与 JWT 的
`perms` 声明），形如：

```json
{
  "role": "ops_member", "role_name": "普通运维",
  "modules": {"cmdb": "read_create", "dict": "read_create", "exec": "read",
              "method": "read", "variable": "read", "credential": "none"},
  "can_execute": true, "can_exec_prod": false
}
```

两个辅助方法：

| 方法 | 用法 | 说明 |
|------|------|------|
| `can(module, action)` | `authStore.can('cloud_account', 'write')` | `action` 取 `read`（默认）/ `create` / `write`，映射到权限档 read / read_create / write |
| `capability(name)` | `authStore.capability('can_exec_prod')` | 独立能力位 |

**快照缺失时回退 `isStaff`**（老 token、`/me/` 还没取到）：否则整站按钮会先全灰一下。

**快照里没有某个模块键时也回退 `isStaff`**，并且 `Layout.vue` 挂载时会 `fetchMe()`
刷一次快照。两者都是为同一件事：快照原来只在登录时取，后端一拆模块（8 → 13），
没重新登录的会话手里那份快照只有旧模块名——按 `none` 处理会把管理员的「用户管理 /
API 凭证 / 变量管理」整块藏掉，而且刷新页面也好不了，只能退出重登。键不存在
≠ 无权限，是「这份快照比该模块老」。

它只用来决定菜单与按钮可见性——**真正的判定始终在后端**，快照过期最多让人多看到
一个按钮，点下去照样被拒。
