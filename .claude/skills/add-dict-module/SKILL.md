---
name: add-dict-module
description: 为 devops-manager 新增一个 CMDB 字典模块（如区服、云账号等基础数据字典）。当用户要求"新增 XX 字典/配置项/基础数据维护"时使用，按项目既定全链路清单实施（模型→API→前端页→测试→文档）。
---

# 新增 CMDB 字典模块

项目已有五个字典模块（项目组/渠道/环境/云厂商/服务类型），全部遵循同一套结构。
新增字典时**逐项完成以下清单**，不要发明新结构。

## 后端（apps/asset_mgr/）

1. **模型** `models.py`：继承 `DictBase`（已含 name/code/enabled/sort_order/remark），
   只加差异字段。`db_table` 用 `asset_<snake_name>`，`ordering = ['sort_order', 'id']`，
   中文 docstring 说明业务含义。
2. **序列化器** `serializers.py`：`fields = '__all__'` 的 ModelSerializer 即可。
3. **视图** `views.py`：继承 `BaseDictViewSet`（自带 enabled/keyword 筛选、
   ProtectedError 友好报错）。若被引用方不是 Server，覆写 `protected_hint`。
4. **路由** `urls.py`：`router.register('<kebab-name>s', XxxViewSet, basename='<kebab-name>')`。
5. **关联**：若 Server 或其他模型需要引用，外键一律 `on_delete=PROTECT, null=True,
   blank=True`；Server 查询筛选沿用「纯数字按 id、否则按 code」约定（见
   `ServerViewSet.DICT_FILTERS`）。
6. **迁移**：`./venv/bin/python manage.py makemigrations asset_mgr && ./venv/bin/python manage.py migrate`。
   需要内置种子数据时用独立数据迁移 + `get_or_create`（幂等，参考 0004）。

## 前端（frontend/src/）

1. `api/assets.js`：`export const xxxApi = createDictApi('<kebab-name>s')`。
2. `views/config/DictManager.vue`：在 `dictConfigs` 加一项（api/label/columns/fields/defaults），
   差异字段用 columns+fields 声明，无差异字段则三者为空。
3. `router/index.js`：加 `config/<kebab-name>s` 路由，复用 DictManager 组件传 `props: { type }`。
4. `views/Layout.vue`：「配置管理」子菜单加菜单项（字典页在上，服务概要在最底部）。

## 验证与文档

1. 测试：在 `apps/asset_mgr/tests.py` 补 CRUD/删除保护/筛选用例，
   跑 `./venv/bin/python manage.py test apps.asset_mgr`。
2. 前端构建：`cd frontend && npm run build`。
3. 文档：更新 `docs/backend/modules/assets.md`（模型表格、接口速览、迁移列表）。

## 注意

- 字典 CRUD 接口统一挂 `/api/assets/` 下，即使前端页面在「配置管理」菜单。
- 涉及批量分配/归属的能力放 `apps/config_mgr`（纯视图 app，不建模型）。
- 删除保护提示必须友好（`「xxx」仍被 N 条记录引用，无法删除`）。
