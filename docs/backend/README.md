# 后端文档索引

> 技术栈：Django 6.0 + DRF + JWT + SQLite(开发) / MySQL(生产)

## 文档目录

| 文档 | 说明 |
|------|------|
| [架构总览](overview.md) | 目录结构、请求生命周期、启动方式 |
| [API 接口文档](api-reference.md) | 所有接口的请求/响应格式 |
| [数据模型](models.md) | 数据库表结构、字段说明、ER 关系 |
| [配置说明](configuration.md) | 环境变量、多环境配置、.env 示例 |
| [认证模块](modules/auth.md) | 自定义用户模型、JWT 登录流程 |
| [资产管理模块](modules/assets.md) | 服务器 CRUD 与 CMDB 字典（项目组/渠道/环境/云厂商） |
| [配置管理模块](modules/config.md) | 服务器归属批量分配、分配概览 |
| [内部导航页模块](modules/portal.md) | 公司内部系统导航页（/nav.html）的区块/系统/导航列/公告配置 |
| [任务执行模块](modules/exec.md) | 命令/剧本执行、动态 inventory、服务规划前置校验 |
| [构建制品与 Jenkins 集成](modules/artifacts.md) | Jenkins 触发构建/日志同步、制品上报（X-API-KEY）、CD 制品变量注入 |
| [审计模块](modules/audit.md) | 中间件自动审计、只读 API |
| [系统配置模块](modules/system.md) | 键值对配置管理 |
| [公共工具](modules/common.md) | 标准分页、统一响应、异常处理 |
