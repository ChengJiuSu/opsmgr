# 云控管理（views/cloud/CloudAccounts.vue · Domains.vue · Certificates.vue）

**路由：** `/cloud/accounts`（仅管理员，`meta.staff`）· `/cloud/domains` · `/cloud/certificates`（登录可读）
**菜单：** 侧边栏「云控管理」子菜单，位于「资产管理」与「配置管理」之间
**API：** `src/api/cloud.js`（`/api/cloud/`，后端模块见 `docs/backend/modules/cloud.md`）

## 云账号（CloudAccounts.vue，仅管理员）

- 列表：名称 / 云厂商 / AK（前 4 后 4 掩码）/ Secret 已设置标志 / 域名数 / 启用 / 备注。
- 新增 / 编辑：厂商下拉来自 CMDB 云厂商字典（提示 DNS 当前支持 aliyun、tencent）；
  Secret 编辑留空表示不修改（后端保持旧值）。
- 测试：调 `test_connection`，成功提示账号下托管域名数量。
- 删除：二次确认；账号下有主域名时后端 400 拒绝。

## 域名管理（Domains.vue）

- 域名表：域名（可点开解析记录）/ 云账号 / 厂商 / 记录数 / 状态
  （正常 success · 异常 danger，同步失败置异常）/ 最近同步 / 备注。
- 导入域名：选云账号拉取厂商侧托管域名，已存在自动跳过。
- 同步：厂商侧全量镜像解析记录到本地（忽略 `_acme-challenge`）。
- 删除：二次确认，明确提示**仅删本地数据，厂商侧解析不受影响**。
- 普通用户只读：隐藏导入/新增/同步/编辑/删除，仅可查看与打开解析记录。

## 解析记录（域名行内展开，默认全部展开可收放）

- 主域名行展开即子域名记录表（同页展示，无需跳转）；子域名列直接显示
  FQDN；记录按域名 id 缓存，同步/增删改后强制刷新。
- 表格列含绑定（服务器 success tag + 主机名，lb/ga/cdn warning tag）、
  同步状态（失败 tooltip 展示错误 + 重推按钮）。
- 新增 / 编辑弹窗：
  - 绑定类型=服务器：服务器下拉（`page_size=0` 全量，公网优先展示 IP），
    记录值由后端按主机 IP 自动填充，仅 A/AAAA；
  - lb/ga/cdn：提示模块未接入，记录值手工填写；
  - 解析线路：default/telecom/unicom/mobile/oversea（腾讯侧中英映射由后端适配层处理）。
- 删除：二次确认，提示**同步删除厂商侧解析，立即生效**；厂商删除失败时本地保留并报错。

## 证书管理（Certificates.vue）

- 列表：名称 / 域名 SAN tags / 验证账号 / 状态（待申请 info · 申请中 warning
  +loading 图标 · 已签发 success · 失败 danger+错误 tooltip）/ 有效期
  （到期日 + 剩余天数 tag：≤14 danger · ≤30 warning）/ 自动续签开关。
- 存在申请中证书时页面 5s 轮询刷新，全部终态自动停。
- 新增：域名多选，建议项按主域名分组（主域名 + `*.` 通配 + 该域名下子域名
  FQDN（A/AAAA/CNAME），可自由输入），首项为主域名（不能是通配符）；
  验证账号只列 aliyun/tencent 云账号。
- 申请 / 续签：确认框说明 DNS 验证流程与耗时（验证记录自动删除）；已签发
  证书域名与验证账号锁定（改域名需新建），备注/自动续签随时可改。
- 下载（仅管理员）：后端打包 zip（`.crt` / `.key` / `.fullchain.pem` + README.txt），
  blob 响应触发单文件下载。
- 删除：确认框说明本平台停止续签、已部署副本不受影响；申请中禁止删除。
