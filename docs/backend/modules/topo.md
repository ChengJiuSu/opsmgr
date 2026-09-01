# 拓扑画布模块（topo_mgr）

**路由前缀：** `/api/topo/`
**App Label：** `topo_mgr`

---

## 职责

把「项目组 × 渠道 × 环境（× 地域）」下的接入链路画成一张可编辑的图：加速域名 →
解析记录 → GA / 负载均衡 → 主机，附带证书落在哪台机上。目的不是画图好看，而是
让「这条链路经过哪些东西」「这台机下线会断哪个域名」有一个能当场看懂的答案。

| 概念 | 说明 |
|------|------|
| `TopoCanvas` | 一张画布，绑定一组维度（项目组/渠道/环境/地域）；同组合允许多张，用 `slug` 区分 |
| `TopoNode` | 画布上的节点，分引用型（`source=ref`）与外部型（`source=external`） |
| `TopoEdge` | 节点之间的有向关系，`relation` 带语义（解析/后端/源站/回源/落地/挂载/关联） |

### 画布不是第五套资产台账

节点分两类，判据是「平台有没有这个东西的模型」：

- **引用型**（`server` / `domain` / `dns_record` / `certificate`）：只存
  `kind + ref_id`，名称、IP、状态一律**实时读**原表。不存快照——主机改了名、证书
  快过期，图上必须当场看见；存快照就变成又一份会过期的台账，而看图的人要的正是
  「现在长什么样」。实体被删则节点标为已失联（`ref.exists=false`）并留在图上，不
  静默消失，否则链路凭空断开且没人知道断在哪。
- **外部型**（GA 加速器、各家 LB、CDN、WAF…）：平台尚未建模的云资源，手工录入
  `label + attrs`。将来 cloud_mgr 补上负载均衡模型，这类节点可就地升级成引用型
  （改 `source/kind/ref_id`，边不用动）。

### 一画布 = 一个维度组合下的一张图

维度口径与 CMDB 一致。`region` 目前是自由文本（与 `Server.region` 同口径，留空 =
跨地域）——等地域进字典再一起改成外键，现在造一个外键会与主机上的文本对不上。

同一组合允许多张图（`ingress-path`、`db-topology`）：一张图塞两件事既排不开版也
没人看得懂。

---

## 文件结构

```
apps/topo_mgr/
├── models.py       # TopoCanvas / TopoNode / TopoEdge
│                   # + 节点类型登记表（REF_KINDS / EXTERNAL_KIND_GROUPS）与关系表
├── graph.py        # 整图取数、整图保存、自动编排；引用型节点的实时解析口径
├── serializers.py  # 画布元信息序列化器（节点与连线不走 ModelSerializer）
├── views.py        # TopoCanvasViewSet（CRUD + graph/autofill/candidates）+ TopoMetaView
├── urls.py         # meta/ 显式路径在前，其余走 DefaultRouter
├── migrations/0001_initial.py
└── tests.py        # 29 用例：模型约束、CRUD 与权限、整图读写、自动编排、候选与元信息
```

RBAC 侧同步改了两处：`apps/auth_mgr/roles.py` 新登记模块 `topo`（归入「资产与
配置」分组，四档全可选），数据迁移 `auth_mgr/0010_grant_topo_module` 给存量角色补
档位（内置角色按出厂矩阵，自定义角色跟随其 `cmdb` 档）。

---

## 接口

| 方法 | 路径 | 说明 | 权限 |
|------|------|------|------|
| GET | `/api/topo/meta/` | 节点类型与关系登记表（左侧调色板数据源） | `topo` 读 |
| CRUD | `/api/topo/canvases/` | 画布元信息，支持 `?project_group=&channel=&env=&region=&keyword=` | `topo` |
| GET | `/api/topo/canvases/{id}/graph/` | 整图读取：节点（含实时状态）+ 连线 | `topo` 读 |
| PUT | `/api/topo/canvases/{id}/graph/` | 整图覆盖保存 | `topo:write` |
| POST | `/api/topo/canvases/{id}/autofill/` | 按维度自动编排，幂等 | `topo:read_create` |
| GET | `/api/topo/canvases/{id}/candidates/` | 可引用的平台资源候选，`?kind=&keyword=` | `topo` 读 |

维度筛选参数「纯数字按 id、否则按 code」，与主机列表同口径。列表与详情都按登录
用户的数据范围（`common/dimscope`）收窄：范围外的画布列表看不到，直接猜 id 也
进不去（`_check_scope`）。

**不叠 sudo**：画布不改变任何人的权限，也不下发任何命令；它引用的实体在各自页面
上该有的门槛一个不少。给一张示意图加二次验证只会让人不用它。

### 整图保存为什么是覆盖式

前端画布的状态是「一堆节点 + 一堆连线」，一次拖拽可能同时改十个节点坐标、删两条
线、加一个节点。拆成 N 个 REST 子接口意味着前端要自己算 diff、还要处理「第 7 个
请求失败而前 6 个已落库」的半保存状态。整图 PUT 一次事务落库，幂等，重放安全。

代价是并发覆盖：两人同时编辑同一张图，后保存的赢。10 人内部平台不值得为此上乐观
锁——真要防，比较一下 `updated_at` 即可，字段已经有了。

**请求体口径**：`nodes` 每项必须带前端生成的 `key`（新节点还没有主键，而连线要
立刻能引用它），`edges` 的 `source`/`target` 引用这个 key；已落库的节点额外回传
`id` 以更新而非重建（主键稳定，影响面反查与审计引用的就是它）。不在入参里的节点
连同其连线一并删除；连线整体重建（它们没有对外身份，重建比算 diff 简单且不会漏）。

---

## 校验规则

| 规则 | 位置 | 说明 |
|------|------|------|
| `source` 与 `ref_id` 自洽 | 模型 `CheckConstraint` + `clean()` + `graph._clean_node_input` | 外部节点带 `ref_id` 会被当引用型去查实体；引用型缺 `ref_id` 则永远查不到 |
| 节点类型必须登记 | `clean()` + 整图入参校验 | 引用型只能用 `REF_KINDS`，外部型只能用 `EXTERNAL_KINDS` |
| 引用的实体必须存在 | `graph._check_refs_exist` | 否则等于允许造死链；报错指名道姓给出缺失 ID |
| 同一实体一图一次 | `UniqueConstraint(canvas, kind, ref_id)` + 保存前中文校验 | 出现两次会让影响面反查数出两条链路，而实际只有一条 |
| 连线不自环、不跨画布 | `CheckConstraint` + `TopoEdge.clean()` | 跨画布的连线在任一张图上都画不出来，属于脏数据 |
| 连线不重复 | `UniqueConstraint(canvas, source, target, relation)` | 同关系的重复连线只是把线画粗，没有信息量 |
| 画布标识唯一 | 序列化器 `validate`（关闭自动校验器） | 自动生成的唯一性校验器会要求 `region` 在 PATCH 里也必须出现 |
| 维度字典删除保护 | 外键 `PROTECT` | 项目组/渠道/环境被画布引用时删不掉 |

节点与连线刻意不用嵌套 serializer：它们只在整图 PUT 里成批出现，要能逐项指出
「第 3 个节点的类型没登记」，用嵌套 serializer 得到的是一坨按下标嵌套的字典，
运维看不懂。

---

## 自动编排（autofill）

按画布维度拉平台已有资源，**只连能确定的关系**，猜不出来的留给人手动画：

| 来源 | 生成 |
|------|------|
| 该维度（含地域）下的 `Server` | 主机节点 |
| `DnsRecord.server` 命中这些主机 | 解析记录节点 + 解析记录 --解析--> 主机 |
| 上述解析记录的 `Domain` | 主域名节点 + 主域名 --解析--> 解析记录 |
| `Certificate.domains` 覆盖这些主域名（含通配符） | 证书节点 + 证书 --关联--> 主域名 |
| `CertDeployment.server` 命中这些主机 | 证书 --落地--> 主机 |

幂等：已在图上的节点与连线跳过，人手摆好的节点不会被移动（新节点按 `kind` 分列
落位，`LAYER_BY_KIND` 即「离用户多远」）。GA / 负载均衡这类中间层由人插在解析与
主机之间。

`ServerService` 不单独出节点，折进主机节点副标题——一台机六个服务就是六个节点，
图会直接炸掉。

---

## 前端

- 列表页：`frontend/src/views/topo/Canvases.vue`，路由 `/topo/canvases`
- 编辑器：`frontend/src/views/topo/CanvasEditor.vue`，路由 `/topo/canvases/:id`
  （URL 可分享定位到某张画布）
- 菜单在「资产管理」下，与服务器台账同组

依赖 `@vue-flow/core`（画布）+ `dagre`（自动排版）。详见
`docs/frontend/views/topo-canvas.md`。

---

## 后续（P1 未做，刻意留给下一期）

- **影响面反查**：给定一台主机/一个域名，反查它在哪些画布上、上下游是谁。边已经
  带语义、`(kind, ref_id)` 已建索引，接口是加法。这是画布真正的运维价值。
- 按维度克隆画布（master → dev 同构环境不用重画）、导入导出 JSON。
- 节点类型字典化（`TopoNodeKind` 表），地域进字典后 `region` 改外键。
- 节点状态实时上色接进概览页异常深链，让画布能当值班大图用。
