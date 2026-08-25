# 构建制品与 Jenkins 集成（artifact_mgr / method_mgr.jenkins）

**路由前缀：** `/api/artifacts/`  
**App Label：** `artifact_mgr`（Jenkins 执行器在 `method_mgr/executors/jenkins.py`）

---

## 职责

打通 **CI（Jenkins 构建）→ 制品上报 → CD（任务流部署）** 全链路：

1. **触发 CI**：任务流中「平台方法」模块选择 `jenkins` provider 的方法，
   调用 Jenkins API 触发指定 job 构建并传参；构建控制台日志增量同步进
   平台执行日志（`TaskExecutionLog`），构建结果映射为模块返回码。
2. **制品上报**：Jenkins CI 完成后由脚本主动上报构建压缩包
   （`POST /api/artifacts/report/`，`X-API-KEY` 鉴权免登录），按
   **项目组/渠道/环境/版本** 唯一登记，DB 记录 md5/大小/Jenkins 构建信息。
3. **CD 取包**：任务流执行时按**模块有效维度**自动查询最新制品，把
   `package_url` / `package_version` / `package_md5` 注入 `--extra-vars`
   （三类模块通用），CD 剧本用 `get_url` 直接拉包并校验 md5；模块
   `params.artifact_version` 可锁定版本（锁定版本不存在则模块失败）。

---

## 系统配置（前端「系统配置」页维护）

| key | 说明 |
|-----|------|
| `jenkins.url` | Jenkins 根地址，如 `http://jenkins.internal:8080`（**直连模式**用） |
| `jenkins.user` | Jenkins 用户名（直连模式用） |
| `jenkins.api_token` | 该用户的 API Token（用户设置 → API Token 生成；直连模式用） |
| `jenkins.agent` | 经 Agent 触发时的默认 Agent 名（manager 在公网、Jenkins 在内网时配置；留空 = 直连） |
| `artifact.api_key` | 制品上报/下载签名 API Key（自定义随机长串；环境变量 `ARTIFACT_API_KEY` 兜底） |

settings：`ARTIFACT_ROOT`（制品落盘根目录，默认 `media/artifacts`）、
`SITE_BASE_URL`（拼接制品下载绝对 URL，**生产必须配置为 CD 主机可达地址**）。

---

## 文件结构

```
apps/artifact_mgr/
├── models.py        # Artifact：三维度+version 唯一；latest_for 取最新/锁定版本；
│                    #   download_sig/download_url：HMAC 签名下载地址
├── permissions.py   # HasArtifactAPIKey：X-API-KEY 与 artifact.api_key 恒定时间比较
├── serializers.py   # 展示 + 上报入参校验（维度按 code/id 解析、版本/文件名白名单防穿越）
├── views.py         # ArtifactViewSet（查/删）+ ArtifactReportView（上报）+ ArtifactDownloadView（下载）
└── urls.py
apps/method_mgr/executors/jenkins.py   # jenkins provider 执行器
apps/system_cfg/services.py            # get_config(key)：系统配置读取
apps/task_exec/runner.py               # _artifact_vars：CD 制品变量注入（extra_vars_args 收口）
```

存储路径：`{ARTIFACT_ROOT}/{项目组code}/{渠道code}/{环境code}/{版本}/{原始文件名}`
（版本与文件名仅允许 `[A-Za-z0-9._-]`，最终路径再经 `resolve_within` 防穿越）。

---

## Jenkins 方法（platform_method，provider=jenkins）

方法是**公共能力，不绑定具体 job**：job 在任务模块参数（`TaskStep.params`）
的保留键 `job_name` 中指定，同一个方法可被不同任务复用触发不同 job。

方法配置 `config`（均可选）：

```json
{"default_params": {"BRANCH": "master"}, "allow_unstable": false, "agent": "nat-gw-01"}
```

任务模块参数 `params`：

```json
{"job_name": "build-app", "BRANCH": "release", "artifact_version": "1.2.3",
 "agent": "nat-gw-01"}
```

- `job_name` 必填（未填模块直接失败并给出提示；存量方法 config 中的
  job_name 仍可作兜底默认值）。
- 构建参数 = `default_params` 被模块 `params` **覆盖合并**；
  `job_name` / `artifact_version` / `agent` 为平台保留键，不透传给 Jenkins。
- 执行流程：`buildWithParameters`（无参 `build`）→ 队列轮询拿 build number
  → `logText/progressiveText` 增量拉日志 → 结果 `SUCCESS`（`allow_unstable`
  时 `UNSTABLE` 亦可）→ rc 0，其余 rc 1；整体受模块 `timeout` 约束
  （**CI 构建耗时长，模块 timeout 需调大**，超时 rc=124 但构建仍在 Jenkins 继续）。
- 认证：API Token Basic Auth（Jenkins 2.96+ 免 CSRF crumb；403 时自动取 crumb 重试）。
- 直连请求 `trust_env=False`：内网 Jenkins 走环境代理只会拿到 502。

### 经 Agent 触发（manager 在公网、Jenkins 在内网）

manager 到不了内网 Jenkins 时，本模块把执行单元打成 `step_type='jenkins'`
下发 ops-agent，由 agent 在内网触发并**流式**回传控制台日志（直连模式是终态
整段返回，经 agent 反而更实时）。

通道解析（`agent_mgr.channel.resolve_service_agent`，四级取第一个命中）：

```
模块参数 params.agent  →  方法配置 config.agent  →  系统配置 jenkins.agent
  →  AgentRoute(任务/模块的项目组 + 环境)  →  直连
```

- 主机三态那套解析用不上：Jenkins 构建不 SSH 任何主机；
- 名字写了却查不到 agent → 模块直接失败（不静默回退直连，那只会换来一次
  连接超时）；
- **凭据不出通道**：payload 只带 `job_name` / `params` / `allow_unstable` /
  `timeout`，Jenkins 连接信息在 agent 主机 `config.yaml` 的 `jenkins:` 段
  维护（与 SSH 私钥同口径）；agent 未配置该段 → 模块 rc=126 并提示；
- agent 侧 rc：0/1 构建成功失败，124 等待超时，143 manager 取消
  （`abort_on_cancel` 默认 true，会连带中止 Jenkins 上的构建），
  126 payload/配置错误，137 agent 重启中断（**Jenkins 构建不随 agent 消亡，
  仍会跑完**）；
- 老 agent（未上报 `capabilities`）不会被拦，由 agent 侧 `task.ack` 拒收
  兜底；上报了能力清单且不含 `jenkins` 的，dispatch 直接 fail fast 提示升级。

制品上报方向不变：Jenkins 仍直接 `POST /api/artifacts/report/` 到公网
manager（需要 Jenkins 所在内网能出公网；不能出网时得另做 agent 代上报）。

## 接口

| 方法 | 路径 | 权限 | 说明 |
|------|------|------|------|
| GET | `/api/artifacts/` | 登录用户 | 列表；`?project_group/channel/env`（id 或 code）、`keyword`（版本/文件名/job） |
| GET | `/api/artifacts/{id}/` | 登录用户 | 详情（含 `download_url`） |
| DELETE | `/api/artifacts/{id}/` | 管理员 | 删除记录并清理磁盘文件 |
| POST | `/api/artifacts/report/` | X-API-KEY | CI 上报（multipart，见下） |
| GET | `/api/artifacts/{id}/download/?sig=…` | 登录用户或签名 | 附件下载；`sig=hmac_sha256(api_key, "{id}:{md5}")` |

### 上报（multipart 字段）

`file`(必填)、`project_group`/`channel`/`environment`(必填，code 或 id)、
`version`(必填)、`md5`(可选，服务端流式计算后比对，不一致丢弃文件返 400)、
`jenkins_job`/`build_number`/`remark`(可选)。同维度同版本已存在返 **409**。

Jenkins 侧上报脚本示例（Pipeline post 阶段）：

```bash
MD5=$(md5sum app.tar.gz | awk '{print $1}')
curl -sf -H "X-API-KEY: ${OPS_API_KEY}" \
  -F "file=@app.tar.gz" \
  -F "project_group=main-game" -F "channel=official" -F "environment=test" \
  -F "version=${BUILD_NUMBER}" -F "md5=${MD5}" \
  -F "jenkins_job=${JOB_NAME}" -F "build_number=${BUILD_NUMBER}" \
  "${OPS_BASE_URL}/api/artifacts/report/"
```

### CD 剧本消费注入变量示例

```yaml
- name: 拉取构建包并校验 md5
  get_url:
    url: "{{ package_url }}"        # 带签名，目标主机免登录
    dest: "/data/packages/app-{{ package_version }}.tar.gz"
    checksum: "md5:{{ package_md5 }}"
```

注入顺序：变量管理 → 制品变量 → 模块额外变量（ansible 同名后者覆盖，
模块手填 `package_url=...` 可强制覆盖）。该维度无制品且未锁版本时静默不注入。

---

## 部署注意

- 大压缩包上传需调大反向代理限制（nginx `client_max_body_size`）。
- `SITE_BASE_URL` 未配置时 `package_url` 为 `http://127.0.0.1:8000`，CD 主机拉包会失败。
- 制品目录随版本增长，暂无自动清理（管理员可在制品管理页删除旧版本）。
