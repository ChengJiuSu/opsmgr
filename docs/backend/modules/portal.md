# 内部导航页模块（portal_mgr）

**路由前缀：** `/api/portal/`
**App Label：** `portal_mgr`

---

## 职责

公司内部系统导航页（`/nav.html`）的配置来源。导航页原先由页面内写死的 JS
数组驱动，改一条系统入口要动前端文件、走一次发布；本模块把整份配置搬进
「配置管理 → 内部导航页」，页面启动时调 `/api/portal/config/` 取回。

| 概念 | 说明 |
|------|------|
| `PortalSetting` | 外观与文案，全局单例（固定 pk=1）：品牌区、报头、侧栏底部说明、布局参数 |
| `PortalBlock` | 内容区块，右侧主区的一个分组；`code` 供导航项与系统卡片引用 |
| `PortalSystem` | 系统清单，页面上的一张卡片；`domain` 唯一（页面按它记 localStorage 的置顶与最近访问） |
| `PortalNavGroup` | 导航列分组，侧栏的一级类别 |
| `PortalNavItem` | 导航项，指向一个内容区块**或**一个纯外链，二选一 |
| `PortalNotice` | 维护公告，生效窗口内进入页面自动弹窗；`code` 供「不再提示本条」记忆 |

### 为什么导航列与内容区块是两套结构

侧栏想少列一项、换个顺序、或挂一个不占内容区块的外链，都不该牵动右侧内容；
反过来，一个内容区块不出现在侧栏也应照常渲染。两者只通过
`PortalNavItem.block` 关联，且该外键允许为空（外链项）。条目序号统一取自
**内容区块的位置**，所以侧栏与正文的编号始终对得上，哪怕两边顺序不同。

---

## 文件结构

```
apps/portal_mgr/
├── models.py       # PortalSetting（单例）/ PortalBlock / PortalSystem
│                   # / PortalNavGroup / PortalNavItem / PortalNotice
├── serializers.py  # 写侧 CRUD 序列化器 + 读侧 build_config() 聚合
├── views.py        # 五个 ViewSet（module_perm('dict')）+ 单例设置 + 聚合读接口
├── urls.py         # config/ 与 setting/ 显式路径在前，其余走 DefaultRouter
├── migrations/
│   ├── 0001_initial.py
│   └── 0002_seed_demo_data.py   # 内置占位清单，幂等 + 可回滚
└── tests.py        # 12 用例：种子、单例、权限、校验、聚合口径
```

---

## 接口

| 方法 | 路径 | 说明 | 权限 |
|------|------|------|------|
| GET | `/api/portal/config/` | 导航页启动取数，整份配置 | 仅需登录 |
| GET / PUT | `/api/portal/setting/` | 外观文案单例 | 读 `dict`；写 `dict:write` |
| CRUD | `/api/portal/blocks/` | 内容区块 | `dict` |
| CRUD | `/api/portal/systems/` | 系统清单，支持 `?block=` | `dict` |
| CRUD | `/api/portal/nav-groups/` | 导航分组（内联返回其导航项） | `dict` |
| CRUD | `/api/portal/nav-items/` | 导航项，支持 `?group=` | `dict` |
| CRUD | `/api/portal/notices/` | 维护公告 | `dict` |

列表接口统一支持 `?keyword=`（按各自的名称/编码/描述模糊匹配）与
`?enabled=true|false`。

### 权限口径为什么不一致

管理接口挂 `module_perm('dict')`，与其它基础字典同档。聚合读接口
`/api/portal/config/` 只要求登录：导航页是全员入口，若也挂 dict 档，没有字典
权限的同事会打不开导航页，等于把一个门户做成了管理员工具；它吐的是内部系统
名称与地址，本就是登录用户可见的信息。

---

## 校验规则

| 规则 | 位置 | 说明 |
|------|------|------|
| 导航项二选一 | `PortalNavItem.clean` + 序列化器 `validate` | 指向区块或填外链，都填/都不填一律拒绝；DRF 不会自动调 `clean()`，两处都要判 |
| 外链项必须有名称 | 同上 | 外链没有区块标题可回落，留空则侧栏出现空条目 |
| 公告时间窗口 | `PortalNotice.clean` + 序列化器 | 结束时间必须晚于开始时间 |
| 域名唯一 | 模型 `UniqueConstraint` | 页面按域名记住置顶与最近访问，重复会串档 |
| 区块删除保护 | `BasePortalViewSet.destroy` | 被系统卡片或导航项 `PROTECT` 引用时返回中文原因，不是 500 |

---

## 聚合读接口的下发口径

`build_config()` 只吐启用中的数据，并额外收敛两处，避免页面出现点不动的元素：

- 导航项指向的区块若被停用，该导航项一并略过；
- 分组下条目全被过滤后，分组本身不下发（否则侧栏留一个空壳分组）。

多行文案在这里拆成数组下发：`masthead_title` 按行拆，`footer` 先按行拆、每行
再按 `**` 分段（奇数段由页面加粗渲染）。公告时间格式化成 `YYYY-MM-DD HH:MM`
（本地时区），与页面的解析逻辑对齐。

---

## 前端

- 管理页：`frontend/src/views/config/Portal.vue`，路由 `/config/portal`，
  菜单在「配置管理」下。五个 Tab 对应五块配置，另有「配置自检」按钮，拉一次
  `/api/portal/config/` 把空区块、无导航项的区块等错配当场列出来。
- 导航页：`frontend/public/nav.html`，静态单页不进 SPA。同源下直接读
  `localStorage.access_token` 调接口，401 跳 `/login?next=`。刻意不做 token
  刷新——只读门户，让用户重登一次比在两处维护刷新逻辑简单。

---

## 内置数据

`0002_seed_demo_data` 写入一份占位清单（9 区块 / 31 系统 / 4 导航分组 /
2 公告）与默认外观文案，全部 `get_or_create`，可重复执行；回滚按内置编码精确
删除，不碰后来手工添加的配置。真实清单上线后直接在管理页改，种子不再参与。
