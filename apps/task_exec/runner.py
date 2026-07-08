"""任务执行分派。

三类任务（command / playbook / method）的执行主机统一来自所选服务器，
执行时生成临时动态 inventory（含服务类型分组），未规划服务的主机拦截：

  - command ：ansible ad-hoc shell。
  - playbook：ANSIBLE_PROJECT_DIR 内既有剧本，ansible-playbook + 动态 inventory。
  - method  ：委托 method_mgr 对应 provider 执行器，动态 inventory 由本模块
              生成后传入（ansible_playbook provider 即「剧本 + 平台管理的 tags」）。
"""
import logging
import os
import re
import shutil
import tempfile

from django.conf import settings

from common.ansible import resolve_within, run_subprocess

logger = logging.getLogger('apps.task_exec')

LOCAL_HOSTS = {'127.0.0.1', 'localhost', '::1'}

# 服务概要中视为「已规划」的状态：planned/deployed 均可执行，offline 不算
PLANNED_STATUSES = ('planned', 'deployed')


def _host_alias(name, ip, used):
    """主机名转 inventory 别名：替换非法字符，空名回退 IP，重名追加 IP 保证唯一。"""
    alias = re.sub(r'[^\w.-]', '_', (name or '').strip()) or ip
    if alias in used:
        alias = f'{alias}_{ip}'
    used.add(alias)
    return alias


def _service_codes_map(servers):
    """查询服务概要，返回 {server_id: [已规划/已部署的服务类型 code]}。"""
    from apps.asset_mgr.models import ServerService

    mapping = {}
    links = ServerService.objects.filter(
        server__in=servers, status__in=PLANNED_STATUSES,
    ).select_related('service_type').order_by('service_type__sort_order', 'service_type_id')
    for link in links:
        mapping.setdefault(link.server_id, []).append(link.service_type.code)
    return mapping


def _unplanned_servers(servers):
    """返回未规划任何服务（无 planned/deployed 记录）的主机列表。"""
    planned = set(_service_codes_map(servers))
    return [s for s in servers if s.id not in planned]


def _inventory_content(servers, credential, workdir):
    """生成 INI 格式动态 inventory（命令任务用）。

    以主机名为 inventory 别名（ansible 日志按主机名显示执行结果），并从服务概要
    注入 service_types 主机变量，同时按服务类型 code 生成分组，供 pattern 圈选。
    """
    lines = []
    key_file = None
    if credential and credential.auth_type == 'key' and credential.private_key:
        key_file = os.path.join(workdir, 'id_key')
        with open(key_file, 'w', encoding='utf-8') as f:
            f.write(credential.private_key.strip() + '\n')
        os.chmod(key_file, 0o600)

    service_map = _service_codes_map(servers)
    groups = {}  # 服务类型 code -> [主机别名]
    used_aliases = set()
    for srv in servers:
        alias = _host_alias(srv.name, srv.ip, used_aliases)
        parts = [alias, f'ansible_host={srv.ip}']
        if srv.ip in LOCAL_HOSTS:
            parts.append('ansible_connection=local')
        else:
            parts.append(f'ansible_port={srv.ssh_port or 22}')
            user = (credential.username if credential else '') or srv.ssh_user or 'root'
            parts.append(f'ansible_user={user}')
            if credential and credential.auth_type == 'password' and credential.password:
                parts.append(f'ansible_ssh_pass={credential.password}')
            if key_file:
                parts.append(f'ansible_ssh_private_key_file={key_file}')
        if credential and credential.become:
            parts.append('ansible_become=true')
            if credential.become_password:
                parts.append(f'ansible_become_pass={credential.become_password}')
        codes = service_map.get(srv.id, [])
        parts.append('service_types="{}"'.format(','.join(codes)))
        for code in codes:
            groups.setdefault(code, []).append(alias)
        lines.append(' '.join(parts))

    for code, aliases in sorted(groups.items()):
        lines.append('')
        lines.append(f'[{code}]')
        lines.extend(aliases)
    return '\n'.join(lines) + '\n'


def _prepare_servers(task):
    """三类任务共用的执行前置：取目标主机并做服务规划兜底校验。

    创建时序列化器已拦截，此处兜底重跑时规划已变更的情况。
    返回 (servers, error)，error 非空表示不允许执行。
    """
    servers = list(task.servers.all())
    if not servers:
        return None, '未选择任何目标主机。'
    unplanned = _unplanned_servers(servers)
    if unplanned:
        names = '、'.join(f'{s.name}({s.ip})' for s in unplanned)
        return None, f'主机 {names} 未规划任何服务类型，不允许执行，请先在「配置管理 → 服务器分配」完成服务规划。'
    return servers, None


def _write_inventory(servers, credential, workdir):
    """在临时工作目录内生成动态 inventory 文件，返回文件路径。"""
    inventory_path = os.path.join(workdir, 'inventory.ini')
    with open(inventory_path, 'w', encoding='utf-8') as f:
        f.write(_inventory_content(servers, credential, workdir))
    return inventory_path


def _playbook_args(task):
    """拼装 playbook / 方法任务共用的 ansible-playbook 追加参数。"""
    args = []
    if (task.tags or '').strip():
        args += ['--tags', task.tags.strip()]
    if (task.limit or '').strip():
        args += ['--limit', task.limit.strip()]
    if (task.extra_vars or '').strip():
        args += ['--extra-vars', task.extra_vars.strip()]
    return args


def _run_command(task):
    servers, error = _prepare_servers(task)
    if error:
        return 1, error
    if not task.command.strip():
        return 1, '命令内容为空。'

    workdir = tempfile.mkdtemp(prefix='ops_cmd_')
    try:
        inventory_path = _write_inventory(servers, task.credential, workdir)
        cmd = ['ansible', task.pattern or 'all', '-i', inventory_path, '-m', 'shell', '-a', task.command]
        return run_subprocess(cmd, timeout=task.timeout or 300)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _run_playbook(task):
    """执行 ANSIBLE_PROJECT_DIR 内的既有剧本，主机来自所选服务器（动态 inventory）。"""
    servers, error = _prepare_servers(task)
    if error:
        return 1, error
    project_dir = getattr(settings, 'ANSIBLE_PROJECT_DIR', '')
    if not project_dir or not os.path.isdir(project_dir):
        return 1, f'Ansible 项目目录不存在：{project_dir!r}（请配置 ANSIBLE_PROJECT_DIR）'
    if not (task.playbook or '').strip():
        return 1, '未指定剧本文件。'
    try:
        playbook_path = resolve_within(project_dir, task.playbook.strip())
    except ValueError as exc:
        return 1, str(exc)
    if not os.path.isfile(playbook_path):
        return 1, f'剧本文件不存在：{task.playbook}'

    workdir = tempfile.mkdtemp(prefix='ops_pb_')
    try:
        inventory_path = _write_inventory(servers, task.credential, workdir)
        cmd = ['ansible-playbook', playbook_path, '-i', inventory_path] + _playbook_args(task)
        return run_subprocess(cmd, timeout=task.timeout or 300, cwd=project_dir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _run_method(task):
    from apps.method_mgr.executors import get_executor

    method = task.method
    if method is None:
        return 1, '未指定平台方法。'
    if not method.enabled:
        return 1, f'方法「{method.name}」已禁用。'
    try:
        executor = get_executor(method.provider)
    except KeyError:
        return 1, f'未知 provider：{method.provider}'

    # 方法任务的主机同样来自平台：生成动态 inventory 交给执行器
    servers, error = _prepare_servers(task)
    if error:
        return 1, error
    workdir = tempfile.mkdtemp(prefix='ops_mtd_')
    try:
        inventory_path = _write_inventory(servers, task.credential, workdir)
        return executor.execute(task, method, inventory_path=inventory_path)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def run(task):
    """执行任务，返回 (return_code, output)。"""
    if task.task_type == 'method':
        return _run_method(task)
    if task.task_type == 'playbook':
        return _run_playbook(task)
    return _run_command(task)
