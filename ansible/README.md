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

## 本地验证

```bash
cd ansible
ansible-playbook site.yml --tags ping --limit local
ansible-playbook site.yml --tags sysinfo --limit local
ansible-playbook site.yml --tags echo -e message=hello
```

## 示例方法（可直接录入平台）

| 方法 | config |
| --- | --- |
| 连通性检查 | `{"playbook": "site.yml", "tags": "ping"}` |
| 采集系统信息 | `{"playbook": "site.yml", "tags": "sysinfo"}` |
| 参数回显演示 | `{"playbook": "site.yml", "tags": "echo"}` |
