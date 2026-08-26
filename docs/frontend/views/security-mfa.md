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
