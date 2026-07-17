"""任务执行分派（任务流）。

任务 = 任务流：模块（TaskStep）按 order 顺序执行，任一模块失败即中止
后续模块；模块内主机按任务 exec_mode 并发/串行（串行 = --forks 1）。

每个模块执行前解析主机并生成临时动态 inventory：
  - 全局范围模块：任务 servers 快照（执行时勾选的全局范围主机）；
  - 自定义范围模块：按模块自己的维度实时匹配（仅已规划服务的主机）。

模块类型：
  - command ：ansible ad-hoc shell。
  - playbook：ANSIBLE_PROJECT_DIR 内既有剧本。
  - method  ：委托 method_mgr 对应 provider 执行器（传入 step，其
              tags/extra_vars/timeout 为模块级、exec_mode 透传任务级）。

run(task) 返回 (return_code, output, steps_result)。
"""
import json
import logging
import os
import re
import shutil
import tempfile
import time

import yaml

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
    """生成 INI 格式动态 inventory。

    以主机名为 inventory 别名（ansible 日志按主机名显示执行结果），并从服务概要
    注入 service_types 主机变量，同时按服务类型 code 生成分组，供剧本圈选。
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


def _write_inventory(servers, credential, workdir):
    """在临时工作目录内生成动态 inventory 文件，返回文件路径。"""
    inventory_path = os.path.join(workdir, 'inventory.ini')
    with open(inventory_path, 'w', encoding='utf-8') as f:
        f.write(_inventory_content(servers, credential, workdir))
    return inventory_path


def _forks_args(task):
    """执行方式参数：串行 = --forks 1 逐台执行；并发用 ansible 默认并发数。"""
    return ['--forks', '1'] if task.exec_mode == 'serial' else []


class ArtifactLookupError(Exception):
    """制品查询失败（如锁定版本不存在），模块应直接失败。"""


def _artifact_vars(step):
    """按模块有效维度查询构建制品，返回注入 CD 剧本的变量字典。

    - step.params.artifact_version 非空：锁定该版本，不存在则抛
      ArtifactLookupError 使模块 fail fast；
    - 未锁定：取该维度最新制品；无制品返回空 dict（静默跳过，不影响非 CD 模块）。
    """
    from apps.artifact_mgr.models import Artifact

    pg, ch, env = _effective_dims(step)
    locked = str((getattr(step, 'params', None) or {}).get('artifact_version') or '').strip()
    artifact = Artifact.latest_for(pg, ch, env, version=locked or None)
    if locked and artifact is None:
        raise ArtifactLookupError(
            f'未找到锁定版本的构建制品：version={locked}（请确认该维度下 CI 已上报此版本）。')
    if artifact is None:
        return {}
    return {
        'package_url': artifact.download_url(),
        'package_version': artifact.version,
        'package_md5': artifact.md5,
    }


def _effective_dims(step):
    """模块生效维度：自定义范围用模块自身维度，否则用任务全局维度。"""
    if getattr(step, 'use_custom_scope', False):
        return step.project_group_id, step.channel_id, step.environment_id
    task = step.task
    return task.project_group_id, task.channel_id, task.environment_id


def extra_vars_args(step):
    """拼装 --extra-vars 参数：按模块有效维度（项目组/渠道/环境精确匹配）
    加载「变量管理」启用变量，YAML 解析后以 JSON 注入；模块自身的额外变量
    作为后一个 -e 传入（ansible 后者覆盖前者同名项）。
    """
    from .models import ExecVariable

    pg, ch, env = _effective_dims(step)
    args = []
    variables = {}
    for var in ExecVariable.objects.filter(
        enabled=True, project_group_id=pg, channel_id=ch, environment_id=env,
    ):
        raw = var.value or ''
        if not raw.strip():
            variables[var.key] = ''
            continue
        try:
            variables[var.key] = yaml.safe_load(raw)
        except yaml.YAMLError:
            variables[var.key] = raw  # 保存时已校验，此处兜底按原文注入
    if variables:
        args += ['--extra-vars', json.dumps(variables, ensure_ascii=False)]
    # CD 制品变量：变量管理之后、模块 extra_vars 之前注入（模块手填可覆盖）
    artifact_vars = _artifact_vars(step)
    if artifact_vars:
        args += ['--extra-vars', json.dumps(artifact_vars, ensure_ascii=False)]
    if (getattr(step, 'extra_vars', '') or '').strip():
        args += ['--extra-vars', step.extra_vars.strip()]
    return args


def _step_servers(step, task):
    """解析模块的执行主机：自定义范围实时匹配，否则用任务全局范围快照。"""
    if step.use_custom_scope:
        return list(step.matched_servers())
    return list(task.servers.all())


def _guard(servers):
    """模块执行前置：主机非空且均已规划服务（兜底执行排队期间规划被撤销）。"""
    if not servers:
        return '未匹配到可执行主机（自定义范围维度无主机，或全局范围未勾选）。'
    unplanned = _unplanned_servers(servers)
    if unplanned:
        names = '、'.join(f'{s.name}({s.ip})' for s in unplanned)
        return f'主机 {names} 未规划任何服务类型，不允许执行，请先在「配置管理 → 服务器分配」完成服务规划。'
    return None


def _run_command_step(step, task, servers):
    if not step.command.strip():
        return 1, '命令内容为空。'
    workdir = tempfile.mkdtemp(prefix='ops_cmd_')
    try:
        inventory_path = _write_inventory(servers, task.credential, workdir)
        cmd = ['ansible', 'all', '-i', inventory_path] + _forks_args(task) \
            + extra_vars_args(step) + ['-m', 'shell', '-a', step.command]
        return run_subprocess(cmd, timeout=step.timeout or 300)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _run_playbook_step(step, task, servers):
    project_dir = getattr(settings, 'ANSIBLE_PROJECT_DIR', '')
    if not project_dir or not os.path.isdir(project_dir):
        return 1, f'Ansible 项目目录不存在：{project_dir!r}（请配置 ANSIBLE_PROJECT_DIR）'
    if not (step.playbook or '').strip():
        return 1, '未指定剧本文件。'
    try:
        playbook_path = resolve_within(project_dir, step.playbook.strip())
    except ValueError as exc:
        return 1, str(exc)
    if not os.path.isfile(playbook_path):
        return 1, f'剧本文件不存在：{step.playbook}'

    workdir = tempfile.mkdtemp(prefix='ops_pb_')
    try:
        inventory_path = _write_inventory(servers, task.credential, workdir)
        cmd = ['ansible-playbook', playbook_path, '-i', inventory_path] + _forks_args(task)
        if (step.tags or '').strip():
            cmd += ['--tags', step.tags.strip()]
        cmd += extra_vars_args(step)
        return run_subprocess(cmd, timeout=step.timeout or 300, cwd=project_dir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _run_method_step(step, task, servers):
    from apps.method_mgr.executors import get_executor

    method = step.method
    if method is None:
        return 1, '未指定平台方法。'
    if not method.enabled:
        return 1, f'方法「{method.name}」已禁用。'
    try:
        executor = get_executor(method.provider)
    except KeyError:
        return 1, f'未知 provider：{method.provider}'

    workdir = tempfile.mkdtemp(prefix='ops_mtd_')
    try:
        inventory_path = _write_inventory(servers, task.credential, workdir)
        # 执行器按 step 读取 tags/extra_vars/timeout（模块级）与 exec_mode（透传任务级）；
        # servers 供不走 Ansible 的 provider（如 jumpserver）直接消费主机对象
        return executor.execute(step, method, inventory_path=inventory_path, servers=servers)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


_STEP_HANDLERS = {
    'command': _run_command_step,
    'playbook': _run_playbook_step,
    'method': _run_method_step,
}

# 执行进度预览上限（与 tasks.OUTPUT_PREVIEW_LIMIT 一致）；暂停确认轮询间隔（秒）
PROGRESS_PREVIEW_LIMIT = 60000
PAUSE_POLL_INTERVAL = 2


def _persist_progress(task, outputs):
    """把已完成模块的输出实时写入任务快照，供前端轮询展示执行进度。

    仅更新 output 字段；最终完整输出与状态由 tasks.execute_task 统一落库。
    """
    from .models import ExecutionTask

    preview = '\n\n'.join(outputs)[-PROGRESS_PREVIEW_LIMIT:]
    ExecutionTask.objects.filter(pk=task.pk).update(output=preview)


def _wait_confirmation(step, task):
    """暂停确认模块：置任务为 paused，轮询确认动作直至继续/中止/超时。

    确认接口（views.confirm）向 pause_action 写 continue/abort，
    本函数消费后清空并把状态恢复为 running。
    """
    from .models import ExecutionTask

    timeout = step.timeout or 300
    ExecutionTask.objects.filter(pk=task.pk).update(status='paused', pause_action='')
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            action = (
                ExecutionTask.objects.filter(pk=task.pk)
                .values_list('pause_action', flat=True)
                .first()
            )
            if action == 'continue':
                return 0, '✔ 已人工确认，继续执行任务流。'
            if action == 'abort':
                return 1, '✖ 确认人选择中止，任务流终止。'
            time.sleep(PAUSE_POLL_INTERVAL)
        return 124, f'等待人工确认超时（>{timeout}s），任务流中止。'
    finally:
        ExecutionTask.objects.filter(pk=task.pk).update(status='running', pause_action='')


def run(task):
    """按 order 顺序执行任务流的全部模块，返回 (return_code, output, steps_result)。

    任一模块失败（rc != 0）即中止，后续模块标记 skipped。
    """
    steps = list(task.steps.all())
    if not steps:
        return 1, '任务未配置任何模块，请编辑任务后重试。', []

    outputs, results = [], []
    total = len(steps)
    failed_rc = None
    for idx, step in enumerate(steps, 1):
        if failed_rc is not None:
            results.append({
                'order': step.order, 'name': step.name, 'task_type': step.task_type,
                'status': 'skipped', 'return_code': None, 'duration': None, 'server_count': None,
            })
            continue

        started = time.monotonic()
        if step.task_type == 'pause':
            # 暂停确认模块：不涉及目标主机，等待人工继续/中止
            servers = []
            header = f'═══ 模块 {idx}/{total}：{step.name}（暂停确认 · 等待人工处理）═══'
            prompt = f'⏸ 任务流已暂停，等待人工确认…（超过 {step.timeout or 300}s 未确认将自动中止）'
            _persist_progress(task, outputs + [f'{header}\n{prompt}'])
            rc, out = _wait_confirmation(step, task)
        else:
            servers = _step_servers(step, task)
            scope = '自定义范围' if step.use_custom_scope else '全局范围'
            header = f'═══ 模块 {idx}/{total}：{step.name}（{step.get_task_type_display()} · {scope} {len(servers)} 台）═══'
            error = _guard(servers)
            if error:
                rc, out = 1, error
            else:
                try:
                    rc, out = _STEP_HANDLERS[step.task_type](step, task, servers)
                except ArtifactLookupError as exc:
                    rc, out = 1, str(exc)
        duration = round(time.monotonic() - started, 2)

        outputs.append(f'{header}\n{out}')
        results.append({
            'order': step.order, 'name': step.name, 'task_type': step.task_type,
            'status': 'success' if rc == 0 else 'failed', 'return_code': rc,
            'duration': duration,
            'server_count': None if step.task_type == 'pause' else len(servers),
        })
        if rc != 0:
            failed_rc = rc
            remaining = total - idx
            if remaining:
                outputs.append(f'✖ 模块「{step.name}」执行失败，中止后续 {remaining} 个模块。')
        # 每个模块结束即持久化进度，前端轮询可见已完成模块的输出
        _persist_progress(task, outputs)

    return (failed_rc if failed_rc is not None else 0), '\n\n'.join(outputs), results
