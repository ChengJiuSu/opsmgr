---
name: add-frontend-page
description: 为 devops-manager 前端（Vue 3 + Element Plus）新增页面或改造现有页面时的项目规范。当用户要求新增管理页面、列表页、弹窗交互，或调整菜单/路由时使用。
---

# 前端页面开发规范（devops-manager/frontend）

技术栈：Vue 3 `<script setup>` + Element Plus + vue-router，无状态管理库依赖复杂化，
JS 变量/函数 camelCase，注释中文。

## 新增页面清单

1. **API 封装** `src/api/<domain>.js`：每个接口一行箭头函数，走 `utils/request`
   （已带 Bearer token 与统一错误提示）。列表响应固定 `res.data.results` / `res.data.total`。
   字典下拉用 `fetchDictOptions(xxxApi)`（assets.js，仅取启用项）。
2. **视图** `src/views/<group>/<Name>.vue`，标准结构自上而下：
   - 概览卡片（可选，点击卡片切换筛选，参考 Allocation.vue）
   - `el-card` 内 toolbar（左筛选右操作按钮）→ `el-table`（`v-loading` + `stripe`）
     → `el-pagination`（`page-sizes: [20, 50, 100]`）
   - 弹窗 `el-dialog` 放模板末尾；批量操作按钮 `:disabled="!selection.length"`
     并在按钮文案中带已选数量
3. **路由** `src/router/index.js`：加 children 项，`meta.title` 中文标题；
   仅管理员页面加 `meta: { staff: true }`（路由守卫已处理）。
4. **菜单** `src/views/Layout.vue`：加 `el-menu-item`，index 与路由 path 一致。
   「配置管理」菜单顺序：项目组 → 渠道 → 云厂商 → 环境 → 服务类型 → 服务概要。

## 交互约定

- 破坏性操作（删除、下线、取消规划）必须 `ElMessageBox.confirm` 二次确认，
  文案说明后果；生产环境相关用 danger 红色标识。
- 批量编辑弹窗**回显当前值**，不用"保持不变"占位：多台不一致显示「多个值（保持不变）」，
  未分配显示「未分配（当前）」，仅提交与基线不同的字段（参考 Allocation.vue 的
  baseline 模式）。
- 状态用 `el-tag` 着色：success=正常/已部署，warning=已规划/维护，
  danger=生产/停止，info=停用/下线/未分配。
- 表格空值统一显示 `未分配` / `—` 并置灰（`.unset` / `.muted` class）。
- 提交按钮加 `:loading="submitting"`；操作成功 `ElMessage.success` 带数量反馈。

## 复用优先

- 字典维护页不新建组件，扩展 `views/config/DictManager.vue` 的 `dictConfigs`。
- 通用样式（toolbar/overview-card）各页面已有现成 scoped CSS，复制保持一致。

## 完成标准

`cd frontend && npm run build` 通过；涉及新页面时更新 `docs/frontend/views/`。
