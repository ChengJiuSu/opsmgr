# 初学者代码理解指南

> 面向第一次接触本项目的开发者。不从文件开始读，而是从「用户行为」倒推代码逻辑。

---

## 核心思路

**先跑起来，亲眼看到它做什么，再回来读代码。**

代码是对「行为」的描述。如果你不知道它要做什么，读代码就像看没有剧情的台词。

---

## 第一步：操作一遍系统

启动前后端后，按顺序做以下操作：

1. 打开 `http://localhost:5173` → 看到登录页
2. 输入 `admin / admin123` → 进入仪表板
3. 点「服务器管理」→ 新增一台服务器 → 保存
4. 点「操作审计」→ 看到刚才新增的操作被自动记录了
5. 点「系统配置」→ 新增一条配置项

**你刚才触发了整个系统的核心链路。** 接下来用代码解释每一步。

---

## 第二步：理解三个基础概念

在读代码之前，先搞懂这三件事。

### 1. 前后端分离是什么

```
后端（Django）只负责：接收请求 → 查数据库 → 返回 JSON 数据
前端（Vue）  只负责：发请求   → 拿到 JSON → 渲染成页面

两者通过 /api/ 接口通信，互不依赖
```

你在浏览器看到的页面是前端画的，页面上的数据是从后端接口拿来的。

### 2. JWT Token 是什么

```
登录成功后，后端给你一张「通行证」（Token）
之后每次请求都在请求头里带上这张通行证
后端验证通行证有效 → 知道你是谁 → 允许操作

Token 长这样（三段 Base64，用 . 分隔）：
eyJhbGciOiJIUzI1NiJ9.eyJ1c2VybmFtZSI6ImFkbWluIn0.xxxxx
↑ 头部（加密算法）      ↑ 载荷（用户信息）            ↑ 签名
```

### 3. Django 的四个核心文件

每个业务模块（如 `apps/asset_mgr/`）都由四个文件组成：

| 文件 | 职责 | 类比 |
|------|------|------|
| `models.py` | 定义数据库表的字段 | 设计表格的列名 |
| `serializers.py` | 把数据库对象转成 JSON | 把表格行翻译成文字 |
| `views.py` | 处理请求，返回响应 | 接待员，决定怎么回应 |
| `urls.py` | 定义哪个路径交给哪个 View | 门牌号 |

---

## 第三步：跟着一次「登录」读代码

这是最好的入口，它把前后端完整串起来。

```
你点击「登录」按钮
│
│  【前端 views/Login.vue】
▼  handleLogin() 被触发，调用 authStore.login()
│
│  【前端 stores/auth.js】
▼  login() 调用 api/auth.js 里的 login 函数
│
│  【前端 utils/request.js】
▼  Axios 发出 POST /api/auth/login/，带上用户名和密码
│
│  【后端 apps/auth_mgr/urls.py】
▼  路由匹配到 LoginView
│
│  【后端 apps/auth_mgr/views.py】
▼  LoginView 调用 CustomTokenObtainPairSerializer 验证密码
│
│  【后端 apps/auth_mgr/serializers.py】
▼  验证成功 → 生成 JWT Token，里面塞入 username、department
│
返回给前端：{ "access": "eyJ...", "refresh": "eyJ..." }
│
│  【前端 stores/auth.js】
▼  把 token 存入 localStorage，解析出 username
│
跳转到 /dashboard
```

**对照这张图，逐个打开对应文件读 10 分钟。**

---

## 第四步：跟着一次「查询列表」读代码

点击「服务器管理」，表格数据是怎么来的：

```
页面加载完毕
│
│  【前端 views/assets/Servers.vue】
▼  onMounted() 触发，调用 fetchData()
│
│  【前端 api/assets.js】
▼  getServers({ page: 1, page_size: 20 })
│
│  【前端 utils/request.js】
▼  Axios 发出 GET /api/assets/servers/?page=1&page_size=20
   请求拦截器自动在请求头加上：Authorization: Bearer eyJ...
│
│  【后端 apps/asset_mgr/urls.py】
▼  路由交给 ServerViewSet
│
│  【后端 apps/asset_mgr/views.py】
▼  ServerViewSet.list() 执行
   get_queryset() 根据 status/env 参数过滤
│
│  【后端 apps/asset_mgr/models.py】
▼  查询数据库：Server.objects.all().order_by('-created_at')
│
│  【后端 common/pagination.py】
▼  StandardPagination 分页处理
│
│  【后端 apps/asset_mgr/serializers.py】
▼  ServerSerializer 把查询结果转成 JSON
│
返回：{ "code": 0, "data": { "total": 5, "results": [...] } }
│
│  【前端 views/assets/Servers.vue】
▼  tableData.value = res.data.results
   表格渲染出来
```

---

## 第五步：理解「自动审计」是怎么做到的

你新增服务器后，操作审计里自动出现了一条记录，这是谁写入的？

```
你发出 POST /api/assets/servers/ 请求
│
▼  Django 中间件栈依次处理请求
   SecurityMiddleware
   CorsMiddleware
   ...
   AuthenticationMiddleware  ← 先识别你是谁
   AuditLogMiddleware        ← 关键！
│
│  【后端 apps/audit/middleware.py】
▼  请求正常放行，交给 ServerViewSet 处理
   ServerViewSet 创建服务器，返回 201 响应
│
▼  响应返回时，AuditLogMiddleware 拦截
   检查：method 是 POST ✅，不是要跳过的路径 ✅
   自动写入一条 AuditLog 记录：
   - user = 当前登录用户
   - path = /api/assets/servers/
   - method = POST
   - status_code = 201
   - ip = 你的 IP 地址
│
▼  审计日志写入数据库，你什么都不用做
```

**中间件就像「自动摄像头」，所有请求经过时自动拍照记录。**

---

## 带着问题读代码

不要从第一行开始读，带着问题找答案效率高 10 倍：

| 我想知道… | 去读这个文件 |
|-----------|-------------|
| 登录接口在哪里定义？ | `apps/auth_mgr/urls.py` + `views.py` |
| Token 是怎么自动带上的？ | `frontend/src/utils/request.js` |
| 操作日志是谁写入的？ | `apps/audit/middleware.py` |
| 分页返回格式是怎么定的？ | `common/pagination.py` |
| 前端怎么控制登录跳转？ | `frontend/src/router/index.js` |
| 服务器表的数据库字段有哪些？ | `apps/asset_mgr/models.py` |
| 前端的全局用户状态存在哪里？ | `frontend/src/stores/auth.js` |
| 后端统一错误格式是怎么处理的？ | `common/response.py` |

---

## 推荐学习顺序

### 第 1 天：跑通环境，操作所有功能

```bash
# 后端
source venv/bin/activate
python manage.py runserver 0.0.0.0:8000

# 前端
cd frontend && npm run dev
```

打开浏览器，把每个功能点一遍，建立直觉。

### 第 2 天：读认证模块（最简单）

```
apps/auth_mgr/models.py       # User 有哪些字段？
apps/auth_mgr/serializers.py  # Token 里为什么有 username？
apps/auth_mgr/views.py        # LoginView 做了什么？
apps/auth_mgr/urls.py         # 路由是怎么定义的？
```

### 第 3 天：读资产管理模块（理解 CRUD）

```
apps/asset_mgr/models.py      # Server 表的字段
apps/asset_mgr/views.py       # 5 个操作怎么实现的（list/create/retrieve/update/destroy）
apps/asset_mgr/serializers.py # 数据怎么转 JSON
apps/asset_mgr/urls.py        # DefaultRouter 自动生成了哪些路由
```

### 第 4 天：读公共工具（理解统一格式）

```
common/pagination.py          # 分页响应为什么长那个样子
common/response.py            # 错误时为什么返回 { code, msg, data }
```

### 第 5 天：读审计中间件（理解中间件机制）

```
apps/audit/middleware.py      # 自动记录是怎么实现的
apps/audit/models.py          # AuditLog 记录了哪些信息
```

### 第 6 天：读前端路由和状态

```
frontend/src/router/index.js  # 路由守卫是怎么拦截未登录的？
frontend/src/stores/auth.js   # login() / logout() 做了什么？
frontend/src/utils/request.js # 请求拦截器和响应拦截器
```

### 第 7 天：读最复杂的页面

```
frontend/src/views/assets/Servers.vue
```

这个页面包含了：列表查询、筛选、分页、新增弹窗、编辑弹窗（复用）、删除确认。  
搞懂这一个，其他页面全都会了。

---

## 常见疑问

**Q：为什么每次请求都要带 Token？**  
A：HTTP 是无状态协议，服务器不记得你上次是谁。Token 相当于每次都出示身份证。

**Q：`models.py` 改了为什么要执行 `makemigrations`？**  
A：模型只是 Python 代码，数据库还不知道你改了什么。`makemigrations` 生成变更记录，`migrate` 才真正修改数据库表结构。

**Q：前端的 `/api/` 请求为什么不会跨域报错？**  
A：开发环境下 Vite 配置了代理，`/api/` 开头的请求被转发到 `localhost:8000`，浏览器以为是同源请求。

**Q：`viewsets.ModelViewSet` 为什么不用写 list/create/update/delete？**  
A：`ModelViewSet` 内置了这 5 个操作的默认实现，只要指定 `queryset` 和 `serializer_class` 就够了。这是 Django REST Framework 的「约定优于配置」设计。

**Q：Pinia 的 `store` 是什么？**  
A：跨组件共享的全局变量。Token 和用户名存在 store 里，任何页面都能直接读取，不需要一层层传参。

---

## 相关文档

- [项目总览 README](../README.md)
- [全局架构图](architecture.md)
- [后端架构总览](backend/overview.md)
- [前端架构总览](frontend/overview.md)
- [API 接口文档](backend/api-reference.md)
- [数据模型](backend/models.md)
