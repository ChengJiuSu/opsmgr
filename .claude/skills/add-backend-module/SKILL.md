---
name: add-backend-module
description: 为 devops-manager 新增后端 Django app 或 API 接口时的项目规范。当用户要求新增业务模块、API 接口、批量操作接口，或扩展 task_exec/method_mgr 执行能力时使用。
---

# 后端模块开发规范（devops-manager）

## 项目结构约定

- 所有业务 app 放 `apps/` 下，路由前缀在 `ops_manager/urls.py` 统一挂载
  （`/api/assets/`、`/api/config/`、`/api/exec/` 等）。
- **纯视图 app**：不建模型、只组合 asset_mgr 等数据源的模块（如 config_mgr、
  dashboard），文件头 docstring 必须声明"本模块不建模型，数据以 xxx 为唯一数据源"。
- Python 代码 snake_case、遵循 PEP 8，注释/文档字符串一律中文。

## 公共组件（必须复用，禁止重复造轮子）

| 组件 | 用途 |
|------|------|
| `common.response.ok / fail` | 统一响应 `{code, msg, data}`；fail 默认 400 |
| `common.permissions.IsAdmin` | 仅管理员（写操作、批量操作） |
| `common.permissions.IsAdminOrReadOnly` | 管理员可写、登录用户只读（字典/资源类） |
| `common.pagination` | 分页，前端固定读 `data.results` / `data.total` |

## API 设计约定

- 资源类接口用 DRF ViewSet + Router；动作类/批量类接口用 APIView + 显式 path。
- APIView 必须加 `@extend_schema(request/responses=OpenApiTypes.OBJECT)`（drf-spectacular）。
- 批量接口请求体统一 `{"server_ids": [...], ...}`，入参逐项校验并返回中文错误。
- 筛选参数：字典外键"纯数字按 id、否则按 code"；布尔用 `'true'/'false'` 字符串。
- 批量写操作包 `transaction.atomic()`；`.update()` 不触发 auto_now，需手动补
  `updated_at=timezone.now()`。
- 状态机类字段：choices + 允许流转表定义在模型上（参考
  `ServerService.STATUS_TRANSITIONS`），视图只做校验不散落规则。
- 外键删除策略：基础数据被引用一律 `PROTECT` + 友好报错；从属记录 `CASCADE`；
  操作人/凭据等弱关联 `SET_NULL`。

## 敏感与高危

- 密钥/密码字段：序列化器 `write_only`，回显用 `has_xxx` 布尔标志（参考 Credential）。
- 生产环境（environment.is_prod）相关的高危操作需要二次确认联动（前端标红已做，
  任务执行侧联动尚未做——实现时从 ExecutionTaskSerializer.validate 入手）。
- 部署类操作先查服务规划：目标主机需有 ServerService planned/deployed 记录
  （参考 `_validate_service_planned`）。

## 完成标准

1. 测试：APIClient 走真实登录（参考各 app tests.py 的 `_client`），覆盖
   正常路径、校验失败、权限（非管理员 403）。
2. `./venv/bin/python manage.py test apps.<app>` 通过。
3. 更新 `docs/backend/modules/<module>.md`（职责/文件结构/接口/校验规则表格）。
4. 有模型变更时迁移文件一并提交；数据迁移必须幂等（get_or_create）且可回滚。
