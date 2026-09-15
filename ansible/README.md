# Ansible 项目目录

平台「剧本任务」的集中执行目录（对应设置 `ANSIBLE_PROJECT_DIR`，默认即本目录）。
平台以本目录为 cwd 调用 `ansible-playbook`，`ansible.cfg` 自动生效。

## 结构

```
ansible/
├── ansible.cfg              # 默认 inventory / roles_path 等
├── site.yml                 # 平台入口剧本：方法 = site.yml + tags
├── inventories/dev/hosts.ini # 主机清单（本项目自行维护，与命令任务的动态 inventory 无关）
└── roles/common_check/       # 示例角色：ping / sysinfo
```

## 约定

- **一个平台方法 = 一个剧本 + 一组 tags**。方法在「方法管理」中配置，
  例如 `{"playbook": "site.yml", "tags": "ping"}`。
- 剧本路径写相对本目录的路径，平台会校验不允许越出本目录。
- inventory 分组名建议与平台服务类型 code 一致，方便 `--limit` 按服务圈选。
- 任务级的 tags / limit / inventory / extra-vars 会覆盖方法默认值。

## 平台注入的服务类型变量

单数与复数是两回事，别混用（`site.yml` 的 `service` tag 有可运行的演示）：

| 变量 | 载体 | 语义 |
| --- | --- | --- |
| `service_type` | extra-vars | **本次任务模块**选定的服务类型，一个 code，平台按「编辑任务时选的服务类型」注入 |
| `service_types` | inventory 主机变量 | **该主机**已规划/已部署的全部服务类型，逗号分隔 |

要「这次动哪个服务」一律用单数 `service_type`，不必再切分复数那个串：

```yaml
- name: restart {{ service_type }}
  ansible.builtin.systemd:
    name: "{{ service_type }}"
    state: restarted
```

注意：

- `service_types` 只存在于平台生成的**动态 inventory**，本目录的静态
  `inventories/dev/hosts.ini` 里没有；
- 暂停模块与未选服务类型的存量模块不注入 `service_type`，剧本里按
  `{{ service_type | default('') }}` 取值，部署类剧本建议再加一道 `assert`；
- `service_type` 是平台保留变量名，不要在「变量管理」里定义同名变量
  （冲突时平台注入值生效）。

## 本地验证

```bash
cd ansible
ansible-playbook site.yml --tags ping --limit local
ansible-playbook site.yml --tags sysinfo --limit local
ansible-playbook site.yml --tags echo -e message=hello
ansible-playbook site.yml --tags service -e service_type=redis
ansible-playbook site.yml --tags service   # 不传时最后一步 assert 会失败，演示前置校验
```

## 示例方法（可直接录入平台）

| 方法 | config |
| --- | --- |
| 连通性检查 | `{"playbook": "site.yml", "tags": "ping"}` |
| 采集系统信息 | `{"playbook": "site.yml", "tags": "sysinfo"}` |
| 参数回显演示 | `{"playbook": "site.yml", "tags": "echo"}` |
| 服务类型变量演示 | `{"playbook": "site.yml", "tags": "service"}` |
