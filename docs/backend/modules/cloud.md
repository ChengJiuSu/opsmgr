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
| `Certificate` | 证书（P2）：domains SAN 列表、acme.sh DNS 验证账号、PEM 存储（私钥不回显） |
| `CertDeployment` | 证书分发目标（P3）：server 走 task_exec 通道，lb/ga/cdn 接口预留 |

---

## 文件结构

```
apps/cloud_mgr/
├── models.py       # CloudAccount / Domain / DnsRecord / Certificate / CertDeployment
├── providers/
│   ├── __init__.py # PROVIDER_REGISTRY + get_dns_provider（按 CloudProvider.code 路由）
│   ├── base.py     # BaseDnsProvider 抽象 + DnsRecordData / DnsProviderError
│   ├── aliyun.py   # 阿里云 AliDNS：requests + RPC 签名 V1（无 SDK 依赖）
│   └── tencent.py  # 腾讯云 DNSPod：requests + TC3-HMAC-SHA256（线路码中英映射）
├── serializers.py  # SK 只写不回显；rr/域名格式校验；唯一性手动校验（value 填充后）
├── views.py        # 三个 ViewSet + import_remote / sync_records / retry_sync / test_connection
└── tests.py        # mock 厂商（FakeDnsProvider），20 用例
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

筛选参数：accounts `provider`（数字 id / code）、`enabled`；domains
`cloud_account`、`keyword`；dns-records `domain`、`type`、`keyword`、`sync_status`。

---

## 校验规则

| 字段 | 规则 |
|------|------|
| Domain.name | 小写化去尾点；RFC 1035 简化正则 |
| DnsRecord.rr | `@`/`*`/`*.xx`/点分标签；`_acme-challenge*` 拒绝手工维护（证书验证流程专用） |
| DnsRecord.ttl | 1~86400 |
| target_type=server | server 必填、仅 A/AAAA；value 自动取 `Server.conn_ip`（公网优先） |
| 唯一性 | 域名+主机记录+类型+值+线路；关闭自动 UniqueTogetherValidator（与 value 后端填充冲突），validate() 手动校验 + DB 约束兜底 |

## 同步语义

- **推送（本地 → 厂商）**：写操作后 `_push`；失败不回滚本地，`sync_status=failed` + `sync_error`，可 retry_sync 重推。启停状态走独立厂商接口。
- **拉取（厂商 → 本地）**：`sync_records` 镜像——按 `remote_record_id` 匹配更新；无 ID 匹配同内容记录时认领；厂商侧已删的本地记录删除；本地 pending/failed 保留；`_acme-challenge` 跳过（acme.sh 验证后自动删，验证窗口期不入库）。

## 后续（P2/P3）

- P2：acme.sh `--issue --dns dns_ali/dns_tencent` celery 任务申请证书；beat 每日扫描 <30 天自动续签。DNS API 模式验证后自动删除 `_acme-challenge` TXT。
- P3：证书分发——server 走 task_exec/ansible 通道（推 PEM + reload 命令）；lb/ga/cdn dispatcher 接口预留。
