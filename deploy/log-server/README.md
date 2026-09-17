# 日志服务端与采集端部署文件

方案与字段口径见 [`docs/log-collection-design.md`](../../docs/log-collection-design.md)。
这里只放部署用的文件。

> **状态：未实机部署验证。** 文件按文档写就，尚未在任何机器上跑过。
> 首次部署必须逐步骤验收（见文末「验收」）。

## 文件

| 文件 | 装到哪 | 说明 |
|---|---|---|
| `victoria-logs.service` | 日志服务器 | VictoriaLogs systemd unit，监听内网 IP:9428 |
| `vector.service` | 每台应用机 | Vector agent systemd unit |
| `vector.yaml` | 每台应用机 | 采集配置，各机差异靠环境变量注入 |
| `vector.env.example` | 每台应用机 | 环境变量模板（部署目录、环境标识、服务端地址） |

> 应用进程（gunicorn / celery / daphne）仍归 supervisor 管，不动。
> 这两个是系统级常驻服务，用 systemd 更顺手，也不该和应用共享重启命运。

## 部署顺序

先服务端，再采集端。反过来的话 agent 起来就开始重试推送，磁盘 buffer 白白吃满。

### 1. 日志服务器

```bash
# 二进制：https://github.com/VictoriaMetrics/VictoriaMetrics/releases（victoria-logs-*）
useradd --system --no-create-home --shell /usr/sbin/nologin vlogs
# 数据目录在 /home/homelab 下，vlogs 要能穿过这一层才进得去
usermod -aG homelab vlogs && chmod g+x /home/homelab
install -m 0755 victoria-logs-prod /usr/local/bin/victoria-logs
install -o vlogs -g vlogs -d /home/homelab/data/victoria-logs
cp victoria-logs.service /etc/systemd/system/            # 改 -httpListenAddr 为本机内网 IP
systemctl daemon-reload && systemctl enable --now victoria-logs
systemctl status victoria-logs
```

**不做认证是已确认的决定**（内网环境）。VictoriaLogs 自身没有认证机制，
访问控制因此全靠网络边界：能连到 9428 的人即可读全部日志，也可往 `/insert/*`
写任意内容。所以这一条要核：

```bash
ss -lntp | grep 9428                 # 现配置 0.0.0.0，即监听所有网卡
sudo iptables -L -n | grep 9428      # 或查云控制台的安全组规则
```

当前 `-httpListenAddr=0.0.0.0:9428`，监听所有网卡。这台机若有公网网卡，
9428 就是直接对公网开的，**安全边界完全落在云安全组 / iptables 上**——
确认 9428 只对内网网段放行，这是目前唯一的防线。

### 2. 每台应用机

```bash
# 二进制：https://vector.dev/docs/setup/installation/
useradd --system --no-create-home --shell /usr/sbin/nologin vector
usermod -aG homelab vector                      # nginx 日志属 adm 组
install -m 0755 vector /usr/local/bin/vector
usermod -aG homelab vector          # data_dir 在 /home/homelab 下，要能穿过这层
install -o vector -g vector -d /home/homelab/apps/vector/data
install -d /etc/vector
cp vector.yaml /etc/vector/
cp vector.env.example /etc/vector/vector.env         # 按本机改
chown root:vector /etc/vector/vector.env && chmod 640 /etc/vector/vector.env

sudo -u vector vector validate /etc/vector/vector.yaml   # 路径是位置参数；须以服务用户跑
cp vector.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now vector
```

**按机器裁剪**：`vector.yaml` 假设本机既有应用又有 nginx。只跑 celery 的机器
（`/home/ubuntu/apps/devops-manager-01`）没有 nginx 也没有 gunicorn，把
`nginx_access` / `nginx_error` / `gunicorn` 三个 source 及其 transform 删掉，
并从 sink 的 `inputs` 里去掉对应项——留着会让 Vector 反复报文件不存在的告警，
真正的告警就淹在里面了。

### 权限

Vector 以 `vector` 用户跑，要能读：

- `${OPS_APP_DIR}/logs/ops_manager-*.log` —— 属 `homelab`/`ubuntu` 用户。
  给目录加 `o+rx`、文件 `o+r` 最省事，但等于对全机可读。更干净的做法是建个
  `logreader` 组，把应用用户与 `vector` 都放进去，目录 `chgrp logreader` + `g+rx`。
- `${OPS_LOG_DIR}/*.log` —— 同上。
- `/var/log/nginx/*` —— 属 `adm` 组，`usermod -aG adm vector` 即可。
- `/home/homelab/apps/vector/data` —— 必须存在且 `vector` 用户可写（检查点 + 磁盘 buffer 都在这里）。
  Vector 会在下面给每个 source 建同名子目录，权限不对就是
  `Could not create subdirectory "app" ... Permission denied`，
  **五个 source 各报一条**，看着像五个问题其实是同一个。
  手动校验必须 `sudo -u vector`，用自己的账号跑验的是错的权限。

> 别图省事把 Vector 跑成 root。它读的文件里有请求路径、用户名，
> 配置里有写入口令；跑成 root 意味着一个配置解析漏洞就是整机。

## 验收

```bash
# 1. 服务端活着，且只绑在内网 IP
systemctl status victoria-logs && ss -lntp | grep 9428

# 2. agent 在推，没有积压
systemctl status vector
journalctl -u vector -n 50 --no-pager

# 3. 数据进来了（LogsQL）
curl -s 'http://vlogs.internal:9428/select/logsql/query' \
     --data-urlencode 'query=_time:5m | stats count() by (host, service)'

# 4. 格式统一是否彻底：解析失败的行有多少
curl -s 'http://vlogs.internal:9428/select/logsql/query' \
     --data-urlencode 'query=_time:1h parse_error:true | stats count() by (host, service)'

# 5. 全链路串联：随便请求一次平台，拿响应头的 X-Request-ID 去查，
#    应该同时查到 nginx、gunicorn、django 三个 service 的记录
curl -s -D- -o /dev/null https://<平台域名>/api/servers/ | grep -i x-request-id
curl -s 'http://vlogs.internal:9428/select/logsql/query' \
     --data-urlencode 'query=request_id:"<上一步的值>"'

# 查询界面（vmui）：http://vlogs.internal:9428/select/vmui/
```

第 4 步不是可选的：`parse_error` 常年不为 0 说明还有日志源没纳入统一格式，
而那部分在服务端是查不动的（只有一坨 `msg` 文本）。

## 已知待定

- **保留期 30 天是拍的**，日志量未实测。跑满一天后看
  `du -sh /home/homelab/data/victoria-logs`，再回头调 `-retentionPeriod`。
- **ops-agent 那台在 NAT 出口**，接法未定（该机直装 Vector 出网 vs 经既有
  WebSocket 通道回传），见设计文档 3.7。
- **任务执行日志（P2）未纳入本配置**，接入前必须先过 `mask_secrets`——
  ansible 输出带下发的凭据。
