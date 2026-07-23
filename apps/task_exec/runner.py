"""任务执行分派（任务流）。

任务 = 任务流：模块（TaskStep）按 order 顺序执行，模块失败后暂停等待
人工处理（重试当前模块 / 跳过继续 / 中止，超时=中止）；模块内主机按任务
exec_mode 并发/串行（串行 = --forks 1）。

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


def _conn_ip(srv):
    """ansible 连接地址：公网优先，NAT 出局主机（无公网 IP）回退私网。"""
    return srv.public_ip or srv.private_ip


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
        conn_ip = _conn_ip(srv)
        alias = _host_alias(srv.name, conn_ip, used_aliases)
        parts = [alias, f'ansible_host={conn_ip}']
        if conn_ip in LOCAL_HOSTS:
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
    """主机并发参数：串行 --forks 1 逐台；并发按任务并发数分批（默认 10）。"""
    if task.exec_mode == 'serial':
        return ['--forks', '1']
    return ['--forks', str(getattr(task, 'forks', None) or 10)]


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
        names = '、'.join(f'{s.name}({s.private_ip})' for s in unplanned)
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
# 模块失败后等待人工处理（重试/跳过/中止）的默认超时（秒），超时按中止处理
FAILURE_WAIT_TIMEOUT_DEFAULT = 600


def _failure_wait_timeout():
    """失败等待超时（秒）：系统配置 task.failure_wait_timeout 优先，其次
    settings.TASK_FAILURE_WAIT_TIMEOUT，默认 600；0 = 不等待（失败即中止）。"""
    from apps.system_cfg.services import get_config

    raw = get_config('task.failure_wait_timeout')
    if str(raw).strip().isdigit():
        return int(raw)
    return getattr(settings, 'TASK_FAILURE_WAIT_TIMEOUT', FAILURE_WAIT_TIMEOUT_DEFAULT)


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
    ExecutionTask.objects.filter(pk=task.pk).update(
        status='paused', pause_action='', pause_type='confirm')
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
        ExecutionTask.objects.filter(pk=task.pk).update(
            status='running', pause_action='', pause_type='')


def _wait_failure_action(task, timeout):
    """模块失败等待人工处理：置任务 paused（pause_type=failed），轮询动作。

    确认接口（views.confirm）向 pause_action 写 retry/skip/abort，本函数
    消费后清空并把状态恢复为 running；等待超时返回 'timeout'（按中止处理）。
    """
    from .models import ExecutionTask

    ExecutionTask.objects.filter(pk=task.pk).update(
        status='paused', pause_action='', pause_type='failed')
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            action = (
                ExecutionTask.objects.filter(pk=task.pk)
                .values_list('pause_action', flat=True)
                .first()
            )
            if action in ('retry', 'skip', 'abort'):
                return action
            time.sleep(PAUSE_POLL_INTERVAL)
        return 'timeout'
    finally:
        ExecutionTask.objects.filter(pk=task.pk).update(
            status='running', pause_action='', pause_type='')


def run(task):
    """按 order 顺序执行任务流的全部模块，返回 (return_code, output, steps_result)。

    模块失败（rc != 0）后任务流暂停，等待人工处理：重试（重跑当前阶段失败
    模块，可反复）/ 跳过（继续后续模块，任务最终仍记失败）/ 中止（后续模块
    标记 skipped）；等待超时按中止处理。暂停确认模块的中止/超时直接终止
    任务流，不进入失败处理。
    """
    steps = list(task.steps.all())
    if not steps:
        return 1, '任务未配置任何模块，请编辑任务后重试。', []

    outputs, results = [], []
    total = len(steps)
    flow_rc = None  # 首个失败模块的 rc（含被跳过的失败），决定任务流最终状态
    aborted = False
    abort_note = ''
    pos = 0  # 已消费的模块数（模块序号 = pos + 组内偏移）
    for stage in _build_stages(steps):
        parallel = len(stage) > 1
        if aborted:
            for step in stage:
                results.append({
                    'order': step.order, 'name': step.name, 'task_type': step.task_type,
                    'status': 'skipped', 'return_code': None, 'duration': None,
                    'server_count': None, 'parallel': parallel,
                })
            pos += len(stage)
            continue

        pairs = [(step, pos + offset) for offset, step in enumerate(stage, 1)]
        entries = _exec_stage(pairs, task, total, outputs, parallel)

        result_by_id = {}
        for step, header, rc, out, duration, server_count in entries:
            outputs.append(f'{header}\n{out}')
            item = {
                'order': step.order, 'name': step.name, 'task_type': step.task_type,
                'status': 'success' if rc == 0 else 'failed', 'return_code': rc,
                'duration': duration, 'server_count': server_count,
                'parallel': parallel, 'attempts': 1,
            }
            results.append(item)
            result_by_id[step.id] = item

        idx_by_id = {step.id: idx for step, idx in pairs}
        failed_entries = [e for e in entries if e[2] != 0]

        # 暂停确认模块的中止/超时：直接终止任务流，不提供重试/跳过
        if failed_entries and stage[0].task_type == 'pause':
            flow_rc = failed_entries[0][2]
            aborted = True
            abort_note = f'模块「{stage[0].name}」暂停确认未通过'
            failed_entries = []

        # 失败处理循环：暂停等待人工选择重试/跳过/中止（超时=中止；
        # 等待超时配置为 0 时不等待，保持「失败即中止」）
        while failed_entries:
            names = '、'.join(e[0].name for e in failed_entries)
            timeout = _failure_wait_timeout()
            if timeout > 0:
                prompt = (f'✖ 模块「{names}」执行失败，任务流已暂停，等待人工处理：'
                          f'重试失败模块 / 跳过并继续 / 中止（超过 {timeout}s 未处理将自动中止）。')
                _persist_progress(task, outputs + [prompt])
                action = _wait_failure_action(task, timeout)
            else:
                action = 'abort_now'
            if action == 'retry':
                attempt = result_by_id[failed_entries[0][0].id]['attempts'] + 1
                outputs.append(f'↻ 已人工确认重试，第 {attempt} 次执行失败模块：{names}。')
                _persist_progress(task, outputs)
                retry_pairs = [(e[0], idx_by_id[e[0].id]) for e in failed_entries]
                entries = _exec_stage(retry_pairs, task, total, outputs, parallel, attempt=attempt)
                for step, header, rc, out, duration, server_count in entries:
                    outputs.append(f'{header}\n{out}')
                    item = result_by_id[step.id]
                    item.update({
                        'status': 'success' if rc == 0 else 'failed', 'return_code': rc,
                        'duration': duration, 'server_count': server_count,
                        'attempts': attempt,
                    })
                failed_entries = [e for e in entries if e[2] != 0]
            elif action == 'skip':
                if flow_rc is None:
                    flow_rc = failed_entries[0][2]
                for e in failed_entries:
                    result_by_id[e[0].id]['skipped_over'] = True
                outputs.append(f'⤼ 已人工确认跳过失败模块「{names}」，继续执行后续模块（本次执行仍记失败）。')
                failed_entries = []
            else:  # abort / timeout / abort_now（未开启失败等待）
                if flow_rc is None:
                    flow_rc = failed_entries[0][2]
                aborted = True
                if action == 'abort_now':
                    abort_note = f'模块「{names}」执行失败'
                else:
                    reason = '等待人工处理超时' if action == 'timeout' else '人工确认中止'
                    abort_note = f'模块「{names}」执行失败（{reason}）'
                failed_entries = []

        pos += len(stage)
        if aborted:
            remaining = total - pos
            if remaining:
                outputs.append(f'✖ {abort_note}，中止后续 {remaining} 个模块。')
        # 每个阶段结束即持久化进度，前端轮询可见已完成模块的输出
        _persist_progress(task, outputs)

    return (flow_rc if flow_rc is not None else 0), '\n\n'.join(outputs), results


def _build_stages(steps):
    """把模块序列切成执行阶段：相邻的 run_parallel 模块合并为一个并行组，
    其余各自成单模块阶段（pause 模块强制独立阶段）。阶段间严格按序。"""
    stages, buf = [], []
    for step in steps:
        if getattr(step, 'run_parallel', False) and step.task_type != 'pause':
            buf.append(step)
            continue
        if buf:
            stages.append(buf)
            buf = []
        stages.append([step])
    if buf:
        stages.append(buf)
    return stages


def _exec_stage(pairs, task, total, outputs, parallel, attempt=1):
    """执行一个阶段（或阶段内失败模块的重试子集），按传入顺序返回 entries。

    pairs 为 (step, 模块序号) 列表；attempt > 1 表示重试轮次（体现在模块头）。
    """
    if not parallel:
        step, idx = pairs[0]
        return [_exec_step(step, task, idx, total, outputs, parallel=False, attempt=attempt)]
    return _exec_parallel_stage(pairs, task, total, attempt)


def _exec_step(step, task, idx, total, outputs, parallel, attempt=1):
    """执行单个模块，返回 (step, header, rc, out, duration, server_count)。

    outputs 仅暂停确认模块用于持久化暂停提示（pause 不会出现在并行组内）。
    """
    started = time.monotonic()
    retry_mark = f' · 重试#{attempt - 1}' if attempt > 1 else ''
    if step.task_type == 'pause':
        # 暂停确认模块：不涉及目标主机，等待人工继续/中止
        header = f'═══ 模块 {idx}/{total}：{step.name}（暂停确认 · 等待人工处理）═══'
        prompt = f'⏸ 任务流已暂停，等待人工确认…（超过 {step.timeout or 300}s 未确认将自动中止）'
        _persist_progress(task, outputs + [f'{header}\n{prompt}'])
        rc, out = _wait_confirmation(step, task)
        server_count = None
    else:
        servers = _step_servers(step, task)
        scope = '自定义范围' if step.use_custom_scope else '全局范围'
        mark = ' · 并行组' if parallel else ''
        header = (f'═══ 模块 {idx}/{total}：{step.name}'
                  f'（{step.get_task_type_display()} · {scope} {len(servers)} 台{mark}{retry_mark}）═══')
        error = _guard(servers)
        if error:
            rc, out = 1, error
        else:
            try:
                rc, out = _STEP_HANDLERS[step.task_type](step, task, servers)
            except ArtifactLookupError as exc:
                rc, out = 1, str(exc)
        server_count = len(servers)
    duration = round(time.monotonic() - started, 2)
    return step, header, rc, out, duration, server_count


def _exec_parallel_stage(pairs, task, total, attempt=1):
    """并行组：组内模块并发执行，等待全部完成后按模块顺序返回结果。

    任一模块失败不打断组内其他模块（各自跑完），失败由调用方汇总处理
    （暂停等待人工重试/跳过/中止）。重试时 pairs 仅含失败模块（序号沿用）。
    """
    from concurrent.futures import ThreadPoolExecutor

    from django.db import connection as dj_connection

    def _worker(step, idx):
        try:
            return _exec_step(step, task, idx, total, [], parallel=True, attempt=attempt)
        finally:
            dj_connection.close()  # 线程私有连接，退出前释放

    with ThreadPoolExecutor(max_workers=len(pairs)) as pool:
        futures = [pool.submit(_worker, step, idx) for step, idx in pairs]
        return [f.result() for f in futures]
