"""任务执行分派。

  - command：基于所选服务器生成临时动态 inventory，ansible ad-hoc shell 执行。
  - method ：委托 method_mgr 对应 provider 执行器（如 ansible_playbook 走集中管理的
             项目目录 + tags）。
"""
import logging
import os
import shutil
import tempfile

from common.ansible import run_subprocess

logger = logging.getLogger('apps.task_exec')

LOCAL_HOSTS = {'127.0.0.1', 'localhost', '::1'}


def _inventory_content(servers, credential, workdir):
    """生成 INI 格式动态 inventory（命令任务用）。"""
    lines = []
    key_file = None
    if credential and credential.auth_type == 'key' and credential.private_key:
        key_file = os.path.join(workdir, 'id_key')
        with open(key_file, 'w', encoding='utf-8') as f:
            f.write(credential.private_key.strip() + '\n')
        os.chmod(key_file, 0o600)

    for srv in servers:
        parts = [srv.ip, f'ansible_host={srv.ip}']
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
        lines.append(' '.join(parts))
    return '\n'.join(lines) + '\n'


def _run_command(task):
    servers = list(task.servers.all())
    if not servers:
        return 1, '未选择任何目标主机。'
    if not task.command.strip():
        return 1, '命令内容为空。'

    workdir = tempfile.mkdtemp(prefix='ops_cmd_')
    try:
        inventory_path = os.path.join(workdir, 'inventory.ini')
        with open(inventory_path, 'w', encoding='utf-8') as f:
            f.write(_inventory_content(servers, task.credential, workdir))
        cmd = ['ansible', task.pattern or 'all', '-i', inventory_path, '-m', 'shell', '-a', task.command]
        return run_subprocess(cmd, timeout=task.timeout or 300)
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
    return executor.execute(task, method)


def run(task):
    """执行任务，返回 (return_code, output)。"""
    if task.task_type == 'method':
        return _run_method(task)
    return _run_command(task)
