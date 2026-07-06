# 审计日志（Logs.vue）

**路由：** `/audit/logs`  
**文件：** `src/views/audit/Logs.vue`

---

## 功能

- 只读展示操作审计日志
- 按 HTTP 方法筛选（GET / POST / PUT / PATCH / DELETE）
- 分页（20 / 50 / 100 条/页）

---

## 组件结构

```
Logs
├── 工具栏（方法筛选）
├── 数据表格
│   ├── 用户名
│   ├── HTTP 方法（Tag 着色）
│   ├── 请求路径（overflow tooltip）
│   ├── 客户端 IP
│   ├── 响应状态码（<400 绿色 / >=400 红色）
│   └── 时间（格式化）
└── 分页
```

---

## 方法 Tag 着色

| 方法 | Tag 类型 |
|------|----------|
| GET | `info` |
| POST | `success` |
| PUT / PATCH | `warning` |
| DELETE | `danger` |

---

## 时间格式化

```javascript
const formatTime = (t) => t?.replace('T', ' ').slice(0, 19)
// "2026-06-09T15:10:00+08:00" → "2026-06-09 15:10:00"
```
