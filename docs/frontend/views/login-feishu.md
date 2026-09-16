# 飞书授权回调页（LoginFeishu.vue）

**路由：** `/login/feishu`（`meta.public`）
**文件：** `src/views/LoginFeishu.vue`

---

## 职责

飞书把用户浏览器带回这里，URL 上带 `code` 与 `state`；本页把两者交给
`POST /api/feishu/sso/callback/` 换平台令牌，成功后 `router.replace('/')`。

**飞书服务器不会访问本页**——授权码是随浏览器回来的。所以这条路由不需要像
`/api/feishu/callback/` 那两条入站入口那样加固，它就是一个普通的前端公开路由。

---

## 两处刻意的处理

**换完令牌用 `replace` 跳走，不用 `push`。** 把带 `code` 的 URL 留在历史记录里，
用户一回退就会拿一个已作废的 code 再试一次，然后看到一个没法自行解释的失败。

**失败原因直接展示后端文案。** 后端把失败写成了可行动的中文（未绑定平台用户、
账号已停用、登录状态已失效、缺通讯录权限拿不到 UnionID），盖成「登录失败」等于
把唯一有用的信息丢掉。

---

## 登录页入口

`Login.vue` 挂载时请求 `GET /api/feishu/sso/status/`（公开接口），
`enabled` 为真才渲染「飞书登录」按钮——避免点了才报错。点击后请求
`GET /api/feishu/sso/authorize/` 拿授权地址并**整页跳转**（飞书授权页不允许被内嵌）。

探测失败时按「未开启」处理：密码登录始终可用，不能因为这个请求挂了把登录页搞挂。
