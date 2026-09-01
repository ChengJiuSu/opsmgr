# API 调用层

---

## HTTP 请求封装（`src/utils/request.js`）

基于 Axios 封装，所有 API 调用统一走此实例。

### 基础配置

```javascript
const request = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || '',  // 开发时为空，走 Vite proxy
  timeout: 10000,                                      // 10 秒超时
})
```

### 请求拦截器

自动附加 JWT Token：

```javascript
config.headers.Authorization = `Bearer ${localStorage.getItem('access_token')}`
```

### 响应拦截器

| 状态码 | 处理方式 |
|--------|----------|
| 2xx | 直接返回 `response.data` |
| 401 | 清除 token → 跳转 `/login` → 提示"登录已过期" |
| 403 | 提示"权限不足" |
| 其他 | 显示后端 `msg` 字段或 `error.message` |

---

## API 函数清单

### 认证（`src/api/auth.js`）

```javascript
login(data)              // POST /api/auth/login/
refreshToken(data)       // POST /api/auth/token/refresh/
```

### 资产管理（`src/api/assets.js`）

```javascript
getServers(params)       // GET    /api/assets/servers/
createServer(data)       // POST   /api/assets/servers/
updateServer(id, data)   // PUT    /api/assets/servers/{id}/
deleteServer(id)         // DELETE /api/assets/servers/{id}/
```

### 拓扑画布（`src/api/topo.js`）

```javascript
getCanvases(params)      // GET    /api/topo/canvases/
createCanvas(data)       // POST   /api/topo/canvases/
updateCanvas(id, data)   // PATCH  /api/topo/canvases/{id}/
deleteCanvas(id)         // DELETE /api/topo/canvases/{id}/
getGraph(id)             // GET    /api/topo/canvases/{id}/graph/      整图读
saveGraph(id, data)      // PUT    /api/topo/canvases/{id}/graph/      整图覆盖保存
autofillCanvas(id)       // POST   /api/topo/canvases/{id}/autofill/   按维度自动编排
getCandidates(id, p)     // GET    /api/topo/canvases/{id}/candidates/ 可引用资源候选
getTopoMeta()            // GET    /api/topo/meta/                     节点类型与关系登记表
cloneCanvas(id, data)    // POST   /api/topo/canvases/{id}/clone/      按新维度克隆
exportCanvas(id)         // GET    /api/topo/canvases/{id}/export/     导出 JSON
importCanvas(data)       // POST   /api/topo/import/                   导入 JSON
getTopoImpact(params)    // GET    /api/topo/impact/                   影响面反查
```

### 审计日志（`src/api/audit.js`）

```javascript
getAuditLogs(params)     // GET /api/audit/logs/
```

### 系统配置（`src/api/system.js`）

```javascript
getConfigs(params)       // GET    /api/system/configs/
createConfig(data)       // POST   /api/system/configs/
updateConfig(id, data)   // PUT    /api/system/configs/{id}/
deleteConfig(id)         // DELETE /api/system/configs/{id}/
```

---

## Vite 开发代理

开发环境下，`/api` 前缀的请求被 Vite Dev Server 代理到后端，避免跨域：

```javascript
// vite.config.js
proxy: {
  '/api': {
    target: 'http://localhost:8000',
    changeOrigin: true,
  },
}
```

生产环境通过 `VITE_API_BASE_URL` 环境变量指定后端地址，或由 Nginx 反向代理处理。

---

## 响应数据结构约定

所有 API 函数返回 `response.data`（axios 拦截器已解包一层），即后端的：

```json
{
  "code": 0,
  "data": { ... }
}
```

在 View 中访问列表数据：

```javascript
const res = await getServers({ page: 1 })
tableData.value = res.data?.results || []
total.value     = res.data?.total   || 0
```
