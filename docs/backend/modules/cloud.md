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
