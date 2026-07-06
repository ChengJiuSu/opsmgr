# 系统配置（Configs.vue）

**路由：** `/system/configs`  
**文件：** `src/views/system/Configs.vue`

---

## 功能

- 系统配置键值对完整 CRUD
- 分页展示
- 新增 / 编辑弹窗复用
- 编辑时 `key` 字段禁用（唯一键不可修改）
- 删除前二次确认

---

## 组件结构

```
Configs
├── 工具栏（新增按钮）
├── 数据表格
│   ├── 配置项（key）
│   ├── 配置值（value，overflow tooltip）
│   ├── 说明（description，overflow tooltip）
│   ├── 更新时间
│   └── 操作（编辑 / 删除）
├── 分页
└── 弹窗（新增/编辑复用）
    └── el-form
        ├── 配置项（key，编辑时 disabled）
        ├── 配置值（textarea，必填）
        └── 说明
```

---

## 核心设计

### 编辑时禁用 key

```html
<el-input v-model="form.key" :disabled="!!editRow" />
```

`key` 为唯一标识符，一旦创建不允许修改，防止业务逻辑混乱。若需修改，应删除后重建。

### 弹窗复用

```javascript
function openDialog(row = null) {
  editRow.value = row
  Object.assign(form, row ? { ...row } : emptyForm())
  dialogVisible.value = true
  formRef.value?.clearValidate()   // 清除上次校验状态
}
```
