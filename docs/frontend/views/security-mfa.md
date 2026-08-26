# 二次认证页（SecurityMfa.vue）

**路由：** `/security/mfa`（name `SecurityMfa`，入口在右上角头像菜单，不进侧边栏）
**文件：** `src/views/SecurityMfa.vue`
**后端：** `docs/backend/modules/auth.md` 的 MFA 章节

---

## 功能

| 状态 | 页面内容 |
|------|---------|
| 未绑定 | 说明 + 「开始绑定」 |
| 绑定中 | 步骤条：① 保存恢复码（复制/下载 txt）→ ② 扫码 + 输码激活 |
| 已启用 | 启用时间、最近验证、剩余恢复码、强制状态；重发恢复码 / 解除绑定 |

被强制且未绑定时页首显示告警，写明当前 `mfa.enforce_scope`。

---

## 关键设计

- **恢复码先于二维码**：先让用户保存恢复码再激活。反过来的话，用户扫完码
  激活成功就直接走了，恢复码那一屏被跳过——真丢手机时才发现没存。
- **二维码前端渲染**：后端只返回 `otpauth://` URI，页面用 `qrcode` 生成
  data URI。后端不引图像库（无 Pillow 依赖）。
- **手动密钥兜底**：扫不了码时可复制密钥手输，页面标注 TOTP/SHA1/6 位/30 秒。
- **激活后换令牌**：`activate` 的响应带一套新令牌（`amr` 含 `otp`），页面写回
  `authStore.persistTokens` 并清掉 `mfa_enrollment_required`。不换的话强制策略
  下当前会话仍会被自己挡住。
- **验证码不对先看时间**：页面直接提示检查手机时间——TOTP 靠时间同步，差超过
  30 秒就算不上，这是最常见的「密钥没错但一直失败」。
- 解绑与重发恢复码走同一个「密码 + 当前验证码」弹窗，重发后新码只显示一次。
- **验证码框带 autofill 契约**（`autocomplete="one-time-code"`、`name="otp"`、
  `inputmode="numeric"`）：绑定完成后 1Password 等管理器就能直接填码，
  激活与敏感操作两处都有。Element Plus 的 `autocomplete` prop 默认 `'off'`，
  不显式传扩展会跳过该字段（详见 `login.md` 的 autofill 契约小节）。

---

## 与路由守卫的配合

```
后端 403 + error_code=mfa_enrollment_required
  → utils/request.js 置 localStorage.mfa_enrollment=1 并跳本页
路由守卫：mfa_enrollment=1 且目标不是本页 → 一律拦到本页
绑定成功 → authStore.setEnrollmentRequired(false) 解锁
```

守卫只是少一次失败请求，真正的强制在后端认证层（`MfaJWTAuthentication`）。

---

## 敏感操作二次验证（sudo 弹窗）

配置面写操作（凭据 / 用户 / Agent 与通道路由 / 云账号 / 系统配置）后端会返回
403 + `error_code=sudo_required`。前端不在各页面各写一遍，而是在拦截器统一处理：

```
utils/request.js 拦到 sudo_required
  → utils/sudo.js requestSudo()（单例 Promise，并发请求共用同一次验证）
  → components/SudoDialog.vue（全局挂在 App.vue）弹框
  → 验证通过 → config._sudoRetried = true → 重放原请求
  → 取消 → 原样 reject
```

要点：

- **弹哪个输入框问后端**：打开时拉 `GET /api/auth/mfa/sudo/` 取 `mfa_enabled`。
  本地缓存的 `user` 可能是刚绑定 MFA 之前的，猜错就会让人白输一遍密码。
- 验证码框同样带 autofill 契约（`one-time-code`），满 6 位自动提交。
- 重放只做一次（`_sudoRetried`），避免后端反复 403 时死循环。
- 绑定成功后页面会 `authStore.fetchMe()`，让别处的 `mfa_enabled` 立刻正确。
