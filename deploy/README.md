# 部署指南

生产架构：nginx（前端静态 + 反向代理）+ gunicorn（Django API）+ MySQL + Redis。
任务执行走 celery 独立 worker（`TASK_EXEC_BACKEND=celery`）：ansible 不在 web
进程里跑，执行多久都不影响后台响应。代码默认值是 thread（本地开发免 Redis），
生产**不要**用它，理由见「任务执行为什么必须走 celery」。

## 目录内容

| 文件 | 用途 |
|------|------|
| `env.example` | 生产环境变量模板，复制为项目根 `.env` |
| `install.sh` | **CD 主路径**：装 Jenkins 制品包，离线装依赖、迁移、重启 |
| `deploy.sh` | 无 CI 场景，首次从源码树部署：venv、联网装依赖、迁移、collectstatic、前端构建 |
| `update.sh` | 无 CI 场景，日常更新：拉代码、迁移、构建、重启服务 |
| `ops-manager.conf` | gunicorn supervisor 配置 |
| `ops-manager-ws.conf` | daphne supervisor 配置（`/ws/` 执行日志推送 + ops-agent 接入） |
| `ops-manager-celery.conf` | celery worker supervisor 配置（默认队列：证书签发/分发） |
| `ops-manager-celery-exec.conf` | **任务执行专用** celery worker（队列 `task_exec`，`TASK_EXEC_BACKEND=celery` 时必须） |
| `ops-manager-celery-beat.conf` | celery beat supervisor 配置（证书自动续签调度，只起一个实例） |
| `docker/` | 后端容器化：Dockerfile + entrypoint（compose 文件在仓库根） |
| `docker/Dockerfile.builder` | CI 依赖构建镜像（预编 wheelhouse / 生成 lock），不进生产 |
| `nginx/` | 前置 nginx（容器化）：docker compose + envsubst 模板 + 安全头片段 + logrotate |
| `nginx/conf/templates/00-default-server.conf.template` | 兜底 server：未显式配置的 Host（含裸 IP）HTTP 返回 444、HTTPS 直接拒握手 |
| `nginx/conf/templates/ops_manager_beian.conf.template` | ICP 备案占位站：另一个域名的静态首页，**不跳转 HTTPS**（审核要求首页直接可访问） |
| `nginx/html/` | 纯静态页目录（备案占位页），挂到容器 `/etc/nginx/html-sites` |

## 依赖交付：CI 预编 wheelhouse，目标机离线装

后端依赖【不在目标机安装】。目标机既不需要 gcc，也不需要访问 PyPI。

```
Jenkins agent（有 docker）                       目标服务器
─────────────────────────────────────            ────────────────────────────
make lock     pip-compile   requirements.lock
make wheels   pip wheel     wheelhouse/*.whl  ─┐
make static   collectstatic staticfiles/       ├─ 打进 tar 制品 ─┐
make frontend vite build    frontend/dist/    ─┘                 │
                                                                 ▼
                                        bash deploy/install.sh
                                          python3.14 -m venv venv
                                          pip install --no-index \
                                            --find-links=wheelhouse \
                                            -r requirements.lock
                                          manage.py migrate
                                          supervisor restart
```

为什么这样切：

- **不在目标机 `pip install -r requirements.txt`**：`mysqlclient` 要编译，生产机
  就得常驻 `gcc` + `default-libmysqlclient-dev`；且每次部署都依赖外网 PyPI，
  网络抖一下部署和回滚一起失败。
- **不打整个 `venv` 进制品**：`venv/bin/*` 的 shebang 是构建机绝对路径，
  `pyvenv.cfg` 指向构建机解释器，C 扩展绑 glibc/Python ABI。能跑，但部署路径
  必须逐字符相同，换机器静默炸。
- **`requirements.lock` 必须存在**：`requirements.txt` 全是下界约束（`>=`），
  同一个 git commit 隔一周部署能解出不同版本的 Django / ansible-core / redis。
  改完 `requirements.txt` 跑 `make lock`，把锁文件一并提交。
- **构建镜像基线必须与生产机一致**（`UBUNTU_VERSION`/`PYTHON_VERSION`）：
  `mysqlclient` 的轮子链接构建机的 MySQL 客户端库，Ubuntu 是
  `libmysqlclient.so.21`、Debian 是 `libmariadb.so.3`，选错只在目标机
  `import MySQLdb` 时才暴露。`install.sh` 的依赖自检会把这种错认出来并给出提示。

### Jenkins / CD 侧约定

CI agent 需要：**docker**（跑构建镜像）。不需要 python3.14、不需要 node ——
后端命令跑在 `Dockerfile.builder` 里，前端跑在 `node:22-bookworm-slim` 里。
`make build USE_DOCKER=false` 是无 docker 时的降级路径，基线不受控，别用于正式构建。

`opsmanager.jenkinsfile` 的 `makeEnv` 可传（都有默认值，一般不用改）：

| 变量 | 默认 | 说明 |
|------|------|------|
| `SKIP_FRONTEND` | `false` | 只改后端时置 `true` 跳过前端构建 |
| `DEPLOY_ENV` | `none` | 写进 `BUILD_INFO`，部署后可核对 |
| `UBUNTU_VERSION` | `24.04` | 构建基线，**必须等于生产机版本** |
| `PYTHON_VERSION` | `3.14` | 同上；改了要重建 wheelhouse 并同步目标机 |
| `NODE_IMAGE` | `node:22-bookworm-slim` | 与 `frontend/package.json` 的 engines 对齐 |

`make build` 的 `output/` 里多了 `wheelhouse/`，共享库步骤 4/5 生成的 md5 清单
会覆盖到每个 `.whl`，即制品完整性也覆盖依赖本身（这也是不带
`--generate-hashes` 仍安全的原因）。

CD 侧（平台的任务流 / artifact_mgr 注入的 `package_*` 变量）落到目标机上是：

```bash
cd /home/ubuntu/apps/devops-manager-01
curl -fsSL "$package_url" -o /tmp/$package_name        # 带签名的下载地址
echo "$package_md5  /tmp/$package_name" | md5sum -c -
tar -xzf /tmp/$package_name -C .
bash deploy/install.sh
```

`.env`、`logs/`、`media/` 不在制品里，解包不会覆盖它们。

### 目标机一次性 bootstrap

```bash
sudo add-apt-repository -y ppa:deadsnakes/ppa && sudo apt-get update
sudo apt-get install -y python3.14 python3.14-venv libmysqlclient21
```

装完之后每次 CD 都不再动系统包。**不需要** node / npm，也不需要
`build-essential` 与 `*-dev` 包。

### 装制品

```bash
cd /home/ubuntu/apps/devops-manager-01
tar -xzf /path/to/ops-manager-<BUILD_TS>.tar.gz -C .
cp deploy/env.example .env && vim .env      # 首次；后续 .env 不随包覆盖
bash deploy/install.sh
```

`install.sh` 的开关：`--deps-only`（只装依赖）、`--no-migrate`、`--no-restart`。
多实例部署用 `SUPERVISOR_PROGRAM=ops-manager-02 bash deploy/install.sh`。

### 本地/CI 常用 make 目标

| 命令 | 作用 |
|------|------|
| `make lock` | 改完 `requirements.txt` 后重新解算 `requirements.lock`（要提交） |
| `make wheels` | 在构建镜像里预编 `wheelhouse/`，并点清数量 |
| `make build` | 完整产物到 `output/`（含 wheelhouse、staticfiles、frontend/dist） |
| `make test` | 在构建基线里跑 `manage.py test apps`（dev settings / sqlite） |
| `make smoke` | 只验证「make 落 output/ → 共享库打包」链路，不装依赖不编前端（原 `make test`） |
| `make build USE_DOCKER=false` | 无 docker 的机器上本地构建（基线不保证，仅开发用） |

## 首次部署

走 Jenkins CD 时用上面的「装制品」流程。本节是**无 CI 场景**的源码树部署
（开发机 / 应急），会联网装依赖、现编 `mysqlclient`、本机构建前端。

前置：python3.14、node22+/npm、nginx、MySQL（已建库 `ops_manager`，utf8mb4）、
`default-libmysqlclient-dev` + `pkg-config` + `build-essential`、
ansible 目标主机 SSH 免密。

```bash
git clone <repo> /opt/devops-manager
cd /opt/devops-manager
cp deploy/env.example .env && vim .env       # SECRET_KEY / ALLOWED_HOSTS / DB / SITE_BASE_URL
bash deploy/deploy.sh

# 后端托管（supervisor，改路径/用户/程序名后安装）
sudo cp deploy/ops-manager.conf /etc/supervisor/conf.d/            # gunicorn
sudo cp deploy/ops-manager-ws.conf /etc/supervisor/conf.d/        # daphne（/ws/ 日志推送）
sudo cp deploy/ops-manager-celery.conf /etc/supervisor/conf.d/     # 证书签发/分发
sudo cp deploy/ops-manager-celery-exec.conf /etc/supervisor/conf.d/  # 任务执行（必须）
sudo supervisorctl reread && sudo supervisorctl update

# 前置 nginx（容器化，见 deploy/nginx/）
cp deploy/nginx/.env.example deploy/nginx/.env
vim deploy/nginx/.env          # 域名、证书路径、OPS_APP_ROOT、上游地址、备案域名
# ⚠ OPS_SERVER_NAME 必须包含 compose healthcheck 里写死的那个 Host，
#   否则健康检查会被兜底 server 444 掉，容器一直 unhealthy
(cd deploy/nginx && docker compose up -d)
sudo cp deploy/nginx/logrotate/ops_manager_nginx /etc/logrotate.d/   # 改日志目录路径
```

### daphne（`/ws/`）不是可选项

`ops-manager-ws.conf` 承载两件事：执行日志的 WebSocket 实时推送、ops-agent 接入。
不部署它，`/ws/` 是 502，前端会退回**每秒一次**的日志轮询接口 —— 恰好在任务执行
期间给后端加压，正是要避免的方向。nginx 侧的 `/ws/` → `127.0.0.1:8001` 分流已内置在
`nginx/conf/templates/ops_manager.conf.template`。

agent 侧部署见独立仓库 ops-agent 的 README 与 `docs/ops-agent-design.md`。

### 任务执行为什么必须走 celery

`TASK_EXEC_BACKEND=thread`（代码默认）把 ansible 子进程和日志汇聚放在 gunicorn
worker 进程里，生产上会有三个后果：

1. **后台整体变慢**：ansible 一跑，那个 worker 就在和 API 抢 GIL 与 CPU。现象是
   全站延迟 + 前端 `timeout of 10000ms exceeded`，而**服务端没有任何报错**
   （超时是浏览器侧断的，服务端只看到连接被对端关闭）。
2. **任务会被静默杀掉**：worker 处理请求期间不向 gunicorn arbiter 打点，请求超过
   `--timeout` 时 arbiter 杀掉整个 worker，住在里面的 ansible 线程一起死，任务
   永远停在 running。
3. **内存翻倍**：ansible 从 web worker fork 出来，worker 越多越吃内存。

切 celery 后 web 进程只建 `TaskExecutionLog` + 投递 Redis，执行全在
`ops-manager-celery-exec` 里。它用**独立队列 `task_exec`**，不与证书签发
（默认 `celery` 队列，acme.sh DNS 验证上限 900s）共用，否则一个正在签发的证书能把
后面排队的 ansible 任务堵十几分钟。

部署路径与配置内路径不一致时，同步修改 `ops-manager.conf`、`ops-manager-ws.conf`、
两个 celery 配置，以及 `nginx/.env` 的 `OPS_APP_ROOT`。

## Docker Compose 部署（后端，可选内置 MySQL）

前置：docker + docker compose v2。前端构建产物仍由宿主机 nginx 提供
（`location ~ ^/(api|admin)/` 反代 `127.0.0.1:8000` 不变）。

⚠ 下面的 `--build` 是**在目标机上构建镜像**：要拉基础镜像、装 apt 包、编
`mysqlclient`，生产机因此变成编译机，且每次构建结果不完全可重复。这条路径
适合开发机与自用环境；正式环境请用「依赖交付」那一节的裸机制品流程，或者
在 CI 里 `docker build` 后推私有 registry，目标机只 `docker compose pull &&
docker compose up -d`（不带 `--build`）。

```bash
cd /path/to/devops-manager
cp deploy/env.example .env && vim .env
```

**方式 A：使用外部 MySQL**（`.env` 的 `DB_HOST` 指向外部库）：

```bash
docker compose up -d --build
```

**方式 B：使用内置 MySQL**（`--profile mysql` 启用，数据落 `mysql-data` 卷）：

```bash
# .env 需设：DB_HOST=mysql、DB_PASSWORD、MYSQL_ROOT_PASSWORD
docker compose --profile mysql up -d --build
```

说明：

- backend 入口自动执行 `migrate` + `collectstatic`，并带 60s 数据库重试等待
  （内置 MySQL 首次初始化较慢，无需 depends_on）。
- 挂载卷：`media`（制品）、`logs`、`ansible`（剧本目录）、`~/.ssh`（只读，
  Ansible 免密私钥）。容器内以 root 运行，SSH 私钥权限需 600。
- 管理员账号：`docker compose exec backend python manage.py createsuperuser`。
- 日常更新：`git pull && docker compose up -d --build`（前端另行 `npm run build`）；
  有 registry 时改成 `docker compose pull && docker compose up -d`。
- 镜像按 `requirements.lock` 装依赖，与裸机制品的版本集合一致。
- 查看日志：`docker compose logs -f backend`。

## 上线后配置

1. 确认数据库迁移已执行（`deploy.sh` / `update.sh` / docker entrypoint 都会自动
   跑；手工部署或只 `git pull` 没跑脚本时必须自己补，见下节）：

   ```bash
   venv/bin/python manage.py showmigrations | grep '\[ \]'   # 应无输出（未应用的迁移）
   venv/bin/python manage.py migrate --noinput
   ```

2. 浏览器登录（deploy.sh 创建的管理员账号）。
3. 「系统管理 → 系统配置」维护 Jenkins 连接：
   `jenkins.url` / `jenkins.user` / `jenkins.api_token`；
   Jenkins 在内网、manager 在公网时改配 `jenkins.agent`（见
   `docs/backend/modules/artifacts.md`）。
4. 跑一个任务验证 Ansible 与 Jenkins 连通。
5. 启用 MFA（二次认证）时按顺序做，别先拉开关：

   ```bash
   # ① .env 配 MFA_SECRET_KEY（留空会从 SECRET_KEY 派生，两者就绑死了，
   #    以后不能单独轮换；换掉它 = 已绑定用户的密钥全部解不开）
   # ② 确认丢设备兜底命令可用（最后一个管理员也丢验证器时只剩这条路）。
   #    用 --check 只读验证，别拿真账号跑真清除——会把对方已绑好的 MFA 清掉
   venv/bin/python manage.py mfa_reset <任一用户名> --check
   # ③ 确认服务器时间准确：TOTP 容差只 ±30s，时钟漂移的现象酷似「密钥不对」
   timedatectl status | grep -i ntp
   ```

   以上就绪并通知到位后，才在「系统配置」把 `mfa.enforce_scope` 从 `off` 改成
   `staff` 或 `all`。改完存量 token 因缺 `amr` 声明会被挡，所有人需重新登录一次。

6. 接入飞书（可选）时**顺序不能反**，见下节。

## 飞书接入

模块设计见 `docs/backend/modules/im_mgr.md`，这里只讲怎么配通。

### 飞书后台要配的四处

在[飞书开放平台](https://open.feishu.cn/app)建**自建应用**，下面四处都要配，
少一处就卡在对应的那一步：

| 位置 | 配什么 | 不配的现象 |
|------|--------|-----------|
| 凭证与基础信息 | App ID / App Secret | 平台取不到 token，`feishu_check` 第 2 项失败 |
| 事件配置 → **加密策略** | Encrypt Key、Verification Token | 地址验证能过，但**正式事件推送全被拒**（飞书不加密就不带签名头） |
| 事件配置 → 订阅方式 | 请求地址（事件订阅 / 卡片回调） | 群里 @机器人没反应 |
| 安全设置 → **重定向 URL** | `https://<域名>/login/feishu` | 飞书登录跳转后报 `20029 Invalid redirect URL` |

**权限**（申请后需管理员审批）：`im:message`、`im:message:send_as_bot`、
`im:chat:readonly`、`contact:user.base:readonly`、`contact:user.email:readonly`，
以及网页免登（`authen`）。

### 配置顺序（有硬依赖）

**飞书后台一保存请求地址就会立刻发一次地址验证**，平台侧没准备好就去保存必然失败，
而回包是恒定的（安全要求），从飞书后台完全看不出原因——只能翻服务端日志。所以：

1. 飞书后台先配好**加密策略**，拿到 Encrypt Key 与 Verification Token。
   请求地址这一步先**空着**。

2. 平台「系统管理 → 飞书接入」录入四项凭据（加密落库，**不进 .env、不进仓库**），
   勾选**启用飞书接入**。缺项时页面会直接列出还差什么。

3. 回调来源 IP 白名单（两层，见下节「收紧」）。**测试期可以都不填**——签名那道是
   独立完整的防护，没有 Encrypt Key 伪造不出请求。

4. **先跑自检**，通过了再回飞书保存地址：

   ```bash
   venv/bin/python manage.py feishu_check                      # 配置 + 出站 + 地址
   venv/bin/python manage.py feishu_check --chats              # 列出机器人所在的群与 chat_id
   venv/bin/python manage.py feishu_check --email a@corp.com   # 顺带验通讯录反查
   venv/bin/python manage.py feishu_check --chat-id oc_xxx     # 真发一张测试卡片
   ```

   它会把要粘到飞书后台的三个地址打出来，并当场验证两种 token、授权页路径与通讯录
   权限。除 `--chat-id` 外全部只读。

5. 把页面上的**事件订阅地址**与**卡片回调地址**贴到飞书后台保存——这一步才触发
   地址验证。

6. 要用飞书登录：平台侧勾**启用飞书登录**，并把**飞书登录回调地址**显式填成
   `https://<域名>/login/feishu`；同一个值加进飞书后台的**安全设置 → 重定向 URL**。

   > 平台侧留空时是按**当前请求的域名**推导的。有人从 IP、内网域名或带端口访问后台
   > 就会推出另一个值，跟飞书白名单对不上，现象随访问方式变化、很难复现。填死它。

7. 开启机器人并登记会话：

   - 飞书后台 → **应用功能 → 机器人** → 启用（不启用时任何 `im/v1/*` 调用都返回
     `Bot ability is not activated`，code 232025）；
   - **版本管理与发布** → 创建版本并发布（自建应用要发布后才能被拉进群）；
   - 飞书群里：群设置 → **群机器人 → 添加机器人** → 选中本应用；
   - `manage.py feishu_check --chats` 列出 `chat_id`（飞书界面上看不到它）；
   - 「系统管理 → 飞书接入 → 会话白名单」登记该 `chat_id`，需要从群里发起执行的
     才勾「允许发起任务执行」。

8. 任务侧逐条开放：任务编辑页勾「允许从飞书发起执行」（默认关，且要求
   「执行时默认全选主机」）。

9. 验证：群里 @机器人 → 出任务下拉 → 选一条非生产任务执行 → 卡片应随执行进度刷新。

### 排错：日志怎么看

回调的回包**恒定**是 `{"code":0}`（回包有差异就是给攻击者的探测反馈），所以飞书后台
永远只显示「Challenge code 没有返回」这类笼统提示。**排错一律看服务端日志**：

```bash
grep -i "飞书回调" logs/*.log | tail -20
```

失败时会把收到了哪些 `X-Lark-*` 头一并打出来，往往直接指出问题在哪：

| 日志 / 现象 | 原因 |
|------|------|
| `缺少签名头 X-Lark-Signature`（正式事件） | 飞书侧没配加密策略；地址验证不受影响（飞书那一步本来就不带签名头） |
| `签名校验失败` | 两边 Encrypt Key 不一致，多半是复制时带了空格 |
| `Verification Token 不匹配` | Token 填错 |
| `解密结果填充非法` | Encrypt Key 对不上的另一种表现 |
| `时间戳偏差 Ns 超过 300s` | 服务器时钟不准，查 `timedatectl status` |
| `来源 IP 不在白名单内` | 应用层白名单没放行飞书出口段 |
| 日志里**什么都没有** | 请求没到 Django。查 nginx：`grep feishu deploy/nginx/logs/ops-manager.api.access.log`，403 = IP 白名单挡住（**空的白名单文件 = 拒绝所有回调**） |
| 浏览器 `20029 Invalid redirect URL` | 飞书后台「安全设置 → 重定向 URL」里没有我们传的那个地址。以**浏览器地址栏里的 `redirect_uri` 参数**为准去对，不要用 `feishu_check` 打印的那行（命令行没有 request，它按 `SITE_BASE_URL` 推导） |

绕过 nginx 直接打后端，判断是哪一层的问题：

```bash
curl -s -X POST http://127.0.0.1:8000/api/feishu/callback/<slug>/event/ \
     -H 'Content-Type: application/json' -d '{}'
# {"code":0} → 到了 Django，看上面的日志表
# 404       → 代码没部署，或 slug 对不上
```

### 上线前收紧

测试期可以先不做限制，**正式启用前这三项必须补上**：

1. **应用层 IP 白名单**：配置页取消勾选「不限制来源 IP」，填飞书出口 IP 段
   （飞书帮助中心「配置企业内网防火墙域名和 IP 白名单」）；
2. **nginx IP 白名单 + 限速**：

   ```bash
   vim deploy/nginx/conf/snippets/feishu_callback_ips.conf   # ⚠ 空文件 = 拒绝所有回调
   docker compose -f deploy/nginx/docker-compose.yaml up -d --force-recreate
   ```

   两层不可互相替代：nginx 管「这条路径根本不该从别处被摸到」，扫描流量在网络层就
   被挡掉；应用层白名单可以改一处配置就生效，不用改 nginx 再 reload。
   顺序上**先填白名单再让配置生效**，反了会把刚配通的回调全部 403 挡死。
3. `.env` 的 `SITE_BASE_URL` 配成真实域名——执行卡片里「查看完整日志」的链接用它。

另外：把对话/工单里出现过的 **App Secret 在飞书后台重置一次**，再到配置页重新录入。

配置页上的「轮换回调地址」会让旧地址**立即失效**，必须同步更新飞书后台，否则所有
回调被拒。

## 日常更新

**走 Jenkins CD（主路径）**：解包新制品后执行

```bash
cd /home/ubuntu/apps/devops-manager-01 && bash deploy/install.sh
```

`install.sh` 依次做：校验 python 基线/`.env`/wheelhouse → 重建 venv 并
**离线**装 `requirements.lock` → 依赖 import 自检 → **`migrate --noinput`** →
重启 supervisor。不联网、不编译、不跑 `collectstatic`（静态与前端随包）。

**无 CI 场景（源码树）**：

```bash
cd /opt/devops-manager && bash deploy/update.sh
```

`update.sh` 依次做：拉代码 → `pip install -r requirements.lock` →
**`migrate --noinput`** → `collectstatic` → 前端构建 → 重启服务。

**不走脚本就必须自己补迁移**。只 `git pull` 加重启是最常见的漏项：新表没建，
接口一调就 `no such table` / `Table doesn't exist`（如 MFA 的 `auth_user_mfa`，
`LoginView` 每次登录都要查它判断是否走第二步，漏了迁移等于全站登不进）。

```bash
venv/bin/pip install -r requirements.lock       # 依赖可能也变了
venv/bin/python manage.py migrate --noinput
venv/bin/python manage.py collectstatic --noinput
```

Docker 部署无需手工执行：backend 入口每次启动都会跑 `migrate`。

## celery 任务后端

生产必须启用（理由见「任务执行为什么必须走 celery」）。

1. 装 Redis，`.env` 设 `TASK_EXEC_BACKEND=celery`、`REDIS_URL=redis://127.0.0.1:6379/0`。
2. **必须部署 worker**（缺了它任务只入队不执行，页面上是一条永远「执行中」的任务
   或永远「申请中」的证书，且没有任何报错）：

   ```bash
   sudo cp deploy/ops-manager-celery.conf /etc/supervisor/conf.d/        # 默认队列：证书签发/分发
   sudo cp deploy/ops-manager-celery-exec.conf /etc/supervisor/conf.d/   # 队列 task_exec：任务执行
   sudo cp deploy/ops-manager-celery-beat.conf /etc/supervisor/conf.d/   # 自动续签调度，可选
   sudo supervisorctl reread && sudo supervisorctl update
   sudo supervisorctl status | grep celery
   ```

   按实际部署改路径、运行用户、`PATH`（须含 `venv/bin` 与 `~/.acme.sh`）与 `HOME`；
   worker 与 web 用同一个运行用户，否则找不到 acme.sh、签出的文件属主也错。
   两个坑：supervisor 的续行是**缩进**不是反斜杠（写 `\` 会当字面参数传给 celery）；
   `stopwaitsecs` 默认 10s 会把正在跑的 acme.sh 砍掉，配置里已给到 930s。
3. 重启 `ops-manager` 服务。
4. 自检：`venv/bin/celery -A ops_manager inspect ping` 有回应，
   `redis-cli -n 0 llen celery` 稳定为 0。

## 常见问题

- **`no such table` / `Table ... doesn't exist`**（如 `auth_user_mfa`）：新版本带了
  迁移但没执行。跑 `venv/bin/python manage.py migrate --noinput`；用
  `showmigrations | grep '\[ \]'` 确认没有未应用的迁移。
- **登录 401 / 静态 404**：nginx `location` 路径与实际部署路径不符，或未 `collectstatic`。
- **任务执行超时被断开**：nginx `proxy_read_timeout` 与 gunicorn `--timeout` 均需 ≥600s，已在配置中给出。
- **执行 ansible 期间整个后台变慢、前端报 `timeout of Nms exceeded`，但服务端无报错**：
  按顺序查这四条 ——
  1. `TASK_EXEC_BACKEND` 是不是还是 `thread`（见上节，这是首要原因）；
  2. 内存。单个 Django 进程 RSS ≈ 116MB，gunicorn worker 数 × 116MB 超过
     `free -m` 的 available 就会换页：**load 很低但请求要等几秒**是换页的典型特征。
     解法是 `--preload`（app 在 fork 前加载一次，worker 间 COW 共享）+ 降 worker 数、
     用 `--threads` 补并发，而不是按核数堆 worker；
  3. 请求耗时到底花在哪：看 nginx 的 `rt=` 与 `urt=`（模板里的 `ops_timing` 格式）。
     `rt` 涨而 `urt` 不涨 = 没有空闲 worker 在排队；两者一起涨 = 单接口自己慢。
     gunicorn 侧 `--access-logformat` 带了 `pid=`，可看出是否只有某个 worker 慢；
  4. ansible 自己把机器打满：`runner.py` 并发模式默认 `--forks 10`，并行组还会
     同时起多个 playbook。核少的机器上给执行 worker 加 `nice -n 10`（已在
     `ops-manager-celery-exec.conf` 里），让 web 在 CPU 争抢中优先。
- **证书一直「申请中」/ 任务点了没反应**（celery 模式）：worker 没跑。
  `venv/bin/celery -A ops_manager inspect ping` 报 `No nodes replied` +
  `redis-cli -n 0 llen celery` 不为 0 即可确认（任务执行看 `llen task_exec`）。
  装上 `ops-manager-celery.conf` / `ops-manager-celery-exec.conf`（见上节）。残留状态用
  `manage.py reap_stuck_cert_tasks` 收成失败；队列里的陈旧任务先
  `redis-cli -n 0 del celery` 清掉，否则 worker 一起来就把过期任务全跑一遍。
- **制品下载地址错误**：`.env` 的 `SITE_BASE_URL` 未配置为对外可达地址。
- **纯 HTTP 内网访问**：prod 配置开启了 `SESSION_COOKIE_SECURE`/`CSRF_COOKIE_SECURE`，
  API 走 JWT 不受影响；如需用 Django admin，需上 HTTPS 或临时关闭这两项。
