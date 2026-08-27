# 云控管理模块（cloud_mgr）

**路由前缀：** `/api/cloud/`
**App Label：** `cloud_mgr`

---

## 职责

云上资源管理。第一期落地域名管理：云账号（厂商 API 凭据）、主域名、
子域名解析记录（增删改即时推送厂商 DNS，本地库为准）。证书管理
（acme.sh DNS 验证申请/续签/分发）模型已建表，链路在 P2/P3 实现。

| 概念 | 说明 |
|------|------|
| `CloudAccount` | 云厂商 API 凭据（AK/SK）；SK 只写不回显（`has_secret_key` 标志）；DNS/证书/后续云资源模块共用 |
| `Domain` | 主域名，归属云账号；可从厂商侧批量导入；`sync_records` 全量镜像解析记录到本地 |
| `DnsRecord` | 解析记录；本地为准即时推送厂商，失败保留本地并标记 `sync_status=failed` 可重推；绑定 target_type（server 自动取主机 IP，lb/ga/cdn 预留） |
| `Certificate` | 证书：domains SAN 列表（首项主域名、支持通配符）、acme.sh DNS 验证签发、PEM 入库（私钥不回显，download 单独接口）、auto_renew |
| `CertDeployment` | 证书分发目标：server 经 ansible 推送 crt/key/fullchain + 执行重载命令；`auto_deploy` 续签后自动重推；lb/ga/cdn 为预留桩 |

---

## 文件结构

```
apps/cloud_mgr/
├── models.py       # CloudAccount / Domain / DnsRecord / Certificate / CertDeployment
├── providers/
│   ├── __init__.py # PROVIDER_REGISTRY + get_dns_provider（按 CloudProvider.code 路由）
│   ├── base.py     # BaseDnsProvider 抽象 + DnsRecordData / DnsProviderError；session trust_env=False
│   ├── aliyun.py   # 阿里云 AliDNS：requests + RPC 签名 V1（无 SDK 依赖）
│   └── tencent.py  # 腾讯云 DNSPod：requests + TC3-HMAC-SHA256（线路码中英映射）
├── acme.py         # acme.sh 封装：--dns dns_ali/dns_tencent 签发、--install-cert 导出、openssl 解析过期
├── deploy.py       # 分发执行层：server（生成 JSON 剧本 + 复用 task_exec inventory）+ lb/ga/cdn 桩
├── tasks.py        # dispatch_issue / dispatch_deploy（thread/celery/sync 与 task_exec 同口径）+ renew_expiring beat 扫描
├── serializers.py  # SK/私钥只写不回显；rr/域名/SAN/部署路径校验；唯一性手动校验（value 填充后）
├── views.py        # 五个 ViewSet + import_remote / sync_records / retry_sync / test_connection / issue / renew / download / deploy / deploy_all
└── tests.py        # mock 厂商（FakeDnsProvider）+ mock acme + mock ansible 子进程，41 用例
```

---

## 接口

| 接口 | 方法 | 权限 | 说明 |
|------|------|------|------|
| `/api/cloud/accounts/` | CRUD | 仅管理员 | 云账号；有域名引用禁止删除 |
| `/api/cloud/accounts/{id}/test_connection/` | POST | 仅管理员 | 凭据连通性测试（拉域名列表） |
| `/api/cloud/domains/` | CRUD | 管理员写/登录读 | 删除仅删本地，不动厂商侧解析 |
| `/api/cloud/domains/import_remote/` | POST | 管理员 | `{"cloud_account": id}`，已存在跳过 |
| `/api/cloud/domains/{id}/sync_records/` | POST | 管理员 | 厂商侧全量镜像到本地；忽略 `_acme-challenge`；失败置域名 abnormal |
| `/api/cloud/dns-records/?domain=` | CRUD | 管理员写/登录读 | 创建/更新/删除即时推送厂商；远端删除失败时本地保留 |
| `/api/cloud/dns-records/{id}/retry_sync/` | POST | 管理员 | 重推 pending/failed 记录 |
| `/api/cloud/certificates/` | CRUD | 管理员写/登录读 | 已签发锁定域名与验证账号；issuing 禁止删除 |
| `/api/cloud/certificates/{id}/issue/` | POST | 管理员 | 后台异步签发（thread/celery，同 TASK_EXEC_BACKEND）；已签发等价强制重签 |
| `/api/cloud/certificates/{id}/renew/` | POST | 管理员 | 强制重签（--force），仅 issued 状态 |
| `/api/cloud/certificates/{id}/download/` | GET | 仅管理员 | zip 包，内含一层 `<名称>/` 目录（`.crt` / `.key` / `.fullchain.pem` + README.txt），`Cache-Control: no-store` |
| `/api/cloud/certificates/{id}/deploy_all/` | POST | 管理员 | 分发到全部已登记目标，跳过分发中与未接入类型（返回 dispatched / skipped） |
| `/api/cloud/dns-change-requests/?status=pending&domain=` | GET | 登录用户 | 高危变更单列表（`status=pending` 只列还能批的，过期的不算）|
| `/api/cloud/dns-change-requests/{id}/approve/` | POST | 管理员 + **sudo** | 审批通过并立即执行；`emergency=true` + `comment` 走紧急自批 |
| `/api/cloud/dns-change-requests/{id}/reject/` | POST | 管理员 + **sudo** | 驳回 |
| `/api/cloud/dns-change-requests/{id}/cancel/` | POST | 申请人或管理员 | 撤销 |
| `/api/cloud/dns-changes/?domain=&record=&action=&keyword=` | GET | 管理员写/登录读 | DNS 变更留痕列表（只读不可删）|
| `/api/cloud/dns-changes/{id}/restore/` | POST | 管理员 + **sudo** | 按前值一键回滚；仅 `before_source='provider'` 的改/删与未撤销的新增可回滚 |
| `/api/cloud/cert-deployments/?certificate=` | CRUD | 管理员写 + **sudo** / 登录读 | 分发目标登记；deploying 禁止删除。`reload_cmd` 是目标主机上执行的任意 shell 命令，登记一条目标等于拿到该主机的远程命令执行面——与「连接凭据」同性质，故写操作要求二次验证 |
| `/api/cloud/cert-deployments/{id}/deploy/` | POST | 管理员（**不要求 sudo**）| 单目标分发（后台异步）；证书未签发 / agent 通道主机 / 未接入类型均拒绝。执行的是已登记配置，需要复核的是配置本身；每次分发都验一次会把「签发后自动分发」变成人工卡点 |

筛选参数：accounts `provider`（数字 id / code）、`enabled`、`keyword`（名称 / AK）；domains
`cloud_account`、`keyword`；dns-records `domain`、`type`、`keyword`、`sync_status`；
cert-deployments `certificate`、`target_type`、`status`。

`keyword` 语义（`_record_keyword_qs`）：主机记录 / 完整子域名 FQDN / 记录值任一
命中。FQDN 无物理列（`DnsRecord.fqdn` 是属性），用 `Concat('rr', '.', 'domain__name')`
注解参与匹配，故搜 `test.example.com` 也能命中 `rr=test` 的记录；记录值参与匹配
支持按 IP 反查。domains 的 `keyword` 额外并上「名下解析记录命中」，因此搜子域名
能直接定位到其主域名行。

---

## 校验规则

| 字段 | 规则 |
|------|------|
| Domain.name | 小写化去尾点；RFC 1035 简化正则 |
| DnsRecord.rr | `@`/`*`/`*.xx`/点分标签；`_acme-challenge*` 拒绝手工维护（证书验证流程专用） |
| DnsRecord.ttl | 1~86400 |
| target_type=server | server 必填、仅 A/AAAA；value 自动取 `Server.conn_ip`（公网优先） |
| 唯一性 | 域名+主机记录+类型+值+线路；关闭自动 UniqueTogetherValidator（与 value 后端填充冲突），validate() 手动校验 + DB 约束兜底 |

> `uniq_dns_record` 是 5 列联合唯一索引，受 InnoDB 单索引 3072 字节上限约束。utf8mb4 下当前占用
> `domain(8) + rr(128×4) + type(10×4) + value(512×4) + line(50×4) = 2808` 字节。
> 加宽 `rr`/`value`/`line` 或往该约束里加列前，先重算这个和；MySQL 无参数可放宽该上限（sqlite 不校验，本地开发不会暴露问题）。

## 同步语义

- **推送（本地 → 厂商）**：写操作后 `_push`；失败不回滚本地，`sync_status=failed` + `sync_error`，可 retry_sync 重推。启停状态走独立厂商接口。
- **拉取（厂商 → 本地）**：`sync_records` 镜像——按 `remote_record_id` 匹配更新；无 ID 匹配同内容记录时认领；厂商侧已删的本地记录删除；本地 pending/failed 保留；`_acme-challenge` 跳过（acme.sh 验证后自动删，验证窗口期不入库）。

## 证书链路（acme.sh）

- **签发**：`acme.sh --issue --dns dns_ali/dns_tencent`（DNS API 模式，AK/SK 注入子进程
  环境变量不落盘；`_acme-challenge` TXT 验证后由 acme.sh 自动删除，解析列表保持干净）。
  成功后 `--install-cert --ecc` 导出到临时目录读 PEM 入库，过期时间 openssl 解析
  （失败回退 +90 天）。子进程清代理环境变量（同 DNS 适配层口径）。
- **调度**：`dispatch_issue` 按 `TASK_EXEC_BACKEND`（thread 默认 / celery / sync），
  状态机 pending/failed → issuing → issued/failed，issuing 禁止重复触发与删除。
- **自动续签**：`cloud_mgr.renew_expiring_certificates`（CELERY_BEAT_SCHEDULE 每 24h），
  auto_renew 且距过期 < `CERT_RENEW_BEFORE_DAYS`(30) 天的 issued 证书强制重签；
  未跑 beat 时手动续签不受影响。
- **配置**（settings/环境变量）：`ACME_BIN`（默认 `~/.acme.sh/acme.sh`，未安装时接口
  报错并提示 `curl https://get.acme.sh | sh`）、`ACME_HOME`（默认 `media/acme`，含私钥
  禁止静态暴露）、`ACME_EMAIL`、`ACME_SERVER`（letsencrypt）、`ACME_TIMEOUT`(900s)。

## 证书分发（deploy.py）

- **服务器**：临时目录写 PEM（0600）→ 生成 JSON 格式剧本（合法 YAML，规避引号
  转义）→ 复用 `task_exec.runner._write_inventory` 生成动态 inventory →
  `ansible-playbook --forks 1` 执行。剧本三步：建目录（0750）、copy 三个文件
  （crt/fullchain 0644、key 0600，`backup: true` 覆盖前留旧副本）、执行重载命令
  （留空则跳过）。执行完临时目录连私钥副本一并清理；输出截尾 4000 字入
  `last_output`。
- **文件名**：`<安全证书名>.crt` / `.key` / `.fullchain.pem`，与下载包同口径
  （`deploy.safe_cert_name`）。
- **agent 通道主机**：v1 不支持（证书内容不出通道，与 method 模块同口径），
  明确报错而非静默降级。
- **续签联动**：`run_issue` 签发成功后顺序执行该证书全部 `auto_deploy=True`
  的目标（同线程串行，避免并发抢同一主机）。
- **lb/ga/cdn**：`DEPLOYERS` 中各自桩函数抛中文 `CertDeployError`；
  `supported_target_types()` 返回已实现类型，视图与前端据此拦截/置灰。
- **路径安全**：部署目录须绝对路径，`/etc`、`/usr`、`/bin` 等系统关键目录直接拒绝。

---

## 秘密存储

`CloudAccount.secret_key`（AccessKey Secret）与 `Certificate.private_key_pem`
（证书私钥）用 `EncryptedTextField` **加密存储**（`common/fields.py`）。

两者都必须可还原——前者要用原值调厂商 API，后者要推到目标主机并打包下载——
所以不能只存哈希；而明文入库意味着「库泄露即云资源被接管 / 域名 HTTPS 可被伪造」。
字段层透明加解密，providers 与 deploy 的读取点零改动。

序列化器层照旧只写不回显（`has_secret_key` / `has_private_key` 布尔）；证书私钥
仅 `download` 动作（管理员）以 zip 形式给出。

**加密迁移** `cloud_mgr/0004_encrypt_cloud_secrets`：幂等且可回滚，口径见
`docs/backend/modules/common.md` 的「透明加密字段」。生产上线前先配
`SECRET_ENC_KEY` 再 migrate。

---

## DNS 变更留痕与回滚（`dns_history.py` / `DnsChangeLog`）

### 为什么要有

删记录原来是「先删云端，再 `record.delete()`」——删完本地行没了，而 audit 表里
DELETE 请求**没有 body**，等于「被删的是什么值」在系统里查不到，只能去云控台翻
厂商操作日志。改也一样：audit 有新值、没有旧值。

审批防的是「改错」，这张表防的是「改错之后」——而且回滚不需要任何人在线。

### 闸门

| 操作 | 权限 |
|------|------|
| 新增解析记录 | 管理员（**不要求 sudo**）——加一条新子域名不会动到现有解析 |
| 改 / 删解析记录 | 管理员 + **sudo 二次验证** |
| 删除主域名 | 管理员 + **sudo**（会连带删掉该域名下全部本地解析镜像，厂商侧不动）|
| 回滚变更 | 管理员 + **sudo** |

改一条 A 记录的影响面（全站流量瞬间被引走）比改一条系统配置大得多，而这里原来
是整个配置面里唯一没有二次验证的写入口。

### 前值从哪来

**一律先从厂商侧拉当前值**，不用本地值：`sync_status=failed` 的记录本地与云端
本就不一致，拉云端才是真实前值。`before_source` 记下来源：

| 来源 | 含义 | 可回滚 |
|------|------|--------|
| `provider` | 云厂商实时值 | ✅ |
| `local` | 拉取失败，退回本地值 | ❌ |
| `missing` | 云端已无此记录（别处删过），本身就是排障线索 | ❌ |
| `none` | 新增（无前值）| 撤销新增可以 |

只信 `provider` 来源：拿本地值去「回滚」可能把一个从未在云端生效过的值推上线。

取前值失败**不打断业务操作**（宽泛兜底 + 告警）——为了一份快照把用户的 DNS 修改
整个打断是本末倒置。同理，留痕写库失败也只告警：DNS 已经改完了，这时抛异常只会
让调用方以为没改成功然后重试一次。

### 回滚语义

| 原动作 | 回滚做什么 |
|--------|-----------|
| `create` | 删掉这条记录（撤销误加）|
| `update` | 把前值写回；记录后来被删了则按前值重建 |
| `delete` | 按前值重建（云端新增会拿到新的 `remote_record_id`）|

**云端先动、本地后动**：云端失败就整体放弃，避免出现「本地显示已回滚、线上还是
错值」——那比不回滚更危险，因为看页面的人会以为已经好了。

回滚自身也落一条 `action='restore'` 的留痕，否则「谁把它改回去的」又查不到。
留痕表**只读不可删**：能被操作人删掉的留痕等于没有留痕。

---

## DNS 高危分级与审批（`dns_risk.py` / `dns_approval.py`）

### 为什么要分级

不是所有 DNS 操作都值得同等对待：加一条新的测试子域名风险很低，改根域 A 记录或
删 MX 是另一个量级。**全都拦会让人把审批当障碍去想办法躲，全都不拦等于没有防线。**

### 高危 = 命中任一条

| 条件 | 理由 |
|------|------|
| 改 / 删已同步的记录 | 新增不算——加一条新记录不会动到线上现有解析 |
| 根域 `@` 记录 | 主域名本身的解析，影响面最大 |
| `NS` / `MX` / `CAA` / `TXT` | NS 改错整个域名失控；MX 改错邮件静默丢失；CAA 影响证书签发；TXT 里躺着 SPF、DKIM 与域名所有权验证。这几类改错的表现都是「过几天才发现」 |
| 记录或域名被标记 `protected` | 人工指定；同步与新建时自动给 `@`/NS/MX 打上 |
| 指向生产主机 | `target_type='server'` 且该主机环境 `is_prod` |

TXT **不自动**打保护标记：它数量多且大量是临时验证记录，自动标会让「保护」这个
标记失去信号价值——它仍然因类型命中高危。

### 流程

高危变更由**解析记录接口自动拦下转单**（`DnsRecordViewSet._approval_gate`），
不另开建单入口，避免出现「绕过记录接口直接建单」的口子。响应是 200 +
`data.pending_approval=true`，前端据此提示「已提交审批」。

高危时**强制填 `reason`**：审批人要据此判断，事后排障最省事的线索也是它。

### 三个决定成败的细节

| 细节 | 落法 |
|------|------|
| **申请人不能自批** | `dns_approval.can_review` 判定，不是靠页面隐藏按钮。10 人团队里最容易被绕过的就是这条 |
| **必须有紧急通道** | `emergency=true` + 必填 `comment`：申请人可自批，日志 `warning` + 列表高亮 + 进审计。没有它，半夜故障没人审批，最后结果是大家互相开超级管理账号——审批流反而降低了安全性 |
| **过期作废** | `DNS_APPROVAL_TTL_HOURS`（默认 24h）。审批时就地标记过期；`expire_dns_requests` 命令批量清理，让列表口径干净 |

### 执行前的漂移比对

申请时记下的线上值与执行时的现值不一致（期间有人改过）就**拒绝执行**，要求撤销
重提。光靠过期时间挡不住 24 小时内的漂移，而闭着眼睛把陈旧目标值推上去，等于用
审批流的名义做了一次没人审过的变更。

比对只在申请时拿到过 `provider` 前值时进行；拉不到当前值时也拒绝执行（宁可等，
不猜）。

执行链路复用 `dns_history`：审批通过后同样落一条变更留痕，回滚入口仍在留痕那边。
