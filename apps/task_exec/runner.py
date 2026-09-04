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
import shlex
import shutil
import tempfile
import time

import yaml

from django.conf import settings

from common.ansible import resolve_within, run_stream

from .live import PREVIEW_LIMIT, SECRET_MASK_MIN_LEN, LiveSink, mask_secrets, push_status

logger = logging.getLogger('apps.task_exec')

LOCAL_HOSTS = {'127.0.0.1', 'localhost', '::1'}

# 服务概要中视为「已规划」的状态：planned/deployed 均可执行，offline 不算
PLANNED_STATUSES = ('planned', 'deployed')

# 临时工作目录内的文件名，同时用作执行现场归档文件名
INVENTORY_FILENAME = 'inventory.ini'
VARS_FILENAME = 'vars.yml'
COMMAND_FILENAME = 'command.txt'


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


def _inventory_content(servers, credential, workdir, include_secrets=True):
    """生成 INI 格式动态 inventory。

    以主机名为 inventory 别名（ansible 日志按主机名显示执行结果），并从服务概要
    注入 service_types 主机变量，同时按服务类型 code 生成分组，供剧本圈选。

    include_secrets=False 用于 agent 通道下发：密码/私钥不出通道，仅保留
    用户/端口/become 标志；SSH 免密凭据驻留 agent 主机（设计文档 3.4）。
    """
    lines = []
    key_file = None
    if include_secrets and credential and credential.auth_type == 'key' and credential.private_key:
        key_file = os.path.join(workdir, 'id_key')
        with open(key_file, 'w', encoding='utf-8') as f:
            f.write(credential.private_key.strip() + '\n')
        os.chmod(key_file, 0o600)

    service_map = _service_codes_map(servers)
    groups = {}  # 服务类型 code -> [主机别名]
    used_aliases = set()
    for srv in servers:
        # 连接地址公网优先（Server.conn_ip），与 JumpServer 注册、前端展示同口径
        conn_ip = srv.conn_ip
        alias = _host_alias(srv.name, conn_ip, used_aliases)
        parts = [alias, f'ansible_host={conn_ip}']
        if conn_ip in LOCAL_HOSTS:
            parts.append('ansible_connection=local')
        else:
            parts.append(f'ansible_port={srv.ssh_port or 22}')
            user = (credential.username if credential else '') or srv.ssh_user or 'root'
            parts.append(f'ansible_user={user}')
            if include_secrets and credential and credential.auth_type == 'password' and credential.password:
                parts.append(f'ansible_ssh_pass={credential.password}')
            if key_file:
                parts.append(f'ansible_ssh_private_key_file={key_file}')
        if credential and credential.become:
            parts.append('ansible_become=true')
            if include_secrets and credential.become_password:
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
    inventory_path = os.path.join(workdir, INVENTORY_FILENAME)
    with open(inventory_path, 'w', encoding='utf-8') as f:
        f.write(_inventory_content(servers, credential, workdir))
    return inventory_path


def step_archive_dir(sink, step, attempt=1):
    """模块的执行现场归档目录：TASK_LOG_DIR/<task_id>/<log_id>.d/step<order>[-retry<n>]。

    sink 未绑定执行记录（直接调 runner.run 的场景，如单测）时返回 None = 不归档。
    与落盘日志同目录同保留期，由 clean_task_logs 一并清理。
    """
    log_id = getattr(sink, 'log_id', None)
    task_id = getattr(sink, 'task_id', None)
    if not log_id or not task_id:
        return None
    name = f'step{step.order}' + (f'-retry{attempt - 1}' if attempt > 1 else '')
    return os.path.join(settings.TASK_LOG_DIR, str(task_id), f'{log_id}.d', name)


def archive_scene(archive_dir, channel, inventory_text, variables, cmd=None, cwd=None,
                  secrets=None):
    """归档一次实际执行的现场：inventory / 变量 / 命令，供事后 debug 原样复现。

    - inventory 一律用**无密钥**版本（不落盘密码与私钥路径），复现时自行补凭据；
    - 敏感变量值打码后落盘：归档是给人排障看的，不需要真值，而这个目录的
      保留期比一次执行长得多（clean_task_logs 默认 30 天）；
    - 命令中的临时工作目录路径改写为归档目录内的同名文件，复制即可重跑；
    - 归档失败只告警，绝不影响执行本体。
    """
    if not archive_dir:
        return None
    target = os.path.join(archive_dir, channel) if channel else archive_dir
    try:
        os.makedirs(target, mode=0o700, exist_ok=True)
        if inventory_text:
            _write_scene_file(target, INVENTORY_FILENAME, inventory_text)
        if variables:
            _write_scene_file(target, VARS_FILENAME,
                              mask_secrets(dump_vars_text(variables), secrets))
        if cmd:
            _write_scene_file(target, COMMAND_FILENAME,
                              mask_secrets(_replay_command(cmd, target, cwd), secrets))
    except OSError as exc:
        logger.warning('执行现场归档失败：%s（%s）', target, exc)
        return None
    return target


def _write_scene_file(target, name, text):
    path = os.path.join(target, name)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    os.chmod(path, 0o600)


def _replay_command(cmd, target, cwd):
    """把实际执行的 argv 渲染成可复制重跑的命令文本。

    临时工作目录内的 inventory/vars 路径替换为归档副本路径（临时目录执行完
    即删除）；cwd 以注释给出，剧本模块需在 Ansible 项目目录内执行。
    """
    replaced = []
    for arg in cmd:
        base = os.path.basename(arg.lstrip('@'))
        if base in (INVENTORY_FILENAME, VARS_FILENAME) and os.path.isabs(arg.lstrip('@')):
            prefix = '@' if arg.startswith('@') else ''
            replaced.append(prefix + os.path.join(target, base))
        else:
            replaced.append(arg)
    header = f'# cwd: {cwd}\n' if cwd else ''
    note = '# inventory 为无密钥版本，复现时请自行补充凭据（ansible_ssh_pass / 私钥）\n'
    return header + note + shlex.join(replaced) + '\n'


def _forks_args(task):
    """主机并发参数：串行 --forks 1 逐台；并发按任务并发数分批（默认 10）。"""
    if task.exec_mode == 'serial':
        return ['--forks', '1']
    return ['--forks', str(getattr(task, 'forks', None) or 10)]


class ArtifactLookupError(Exception):
    """制品查询失败（如锁定版本不存在），模块应直接失败。"""


def collect_dim_variables(project_group_id, channel_id, environment_id, include_secrets=True):
    """按维度（项目组/渠道/环境精确匹配）收集「变量管理」启用变量。

    一条变量记录 = 一个 ansible 变量：变量名取 key，变量值取 YAML 解析结果
    （值为空 → 空串；解析失败 → 原文兜底，保存时已校验，此处仅防御）。

    **敏感变量（is_secret）按字面字符串注入，不过 YAML 解析**：密码里的 `#`
    会被 YAML 当注释截断、`k: v` 会被解析成字典，这类静默改值比报错难查太多。
    解密失败直接抛 DecryptError——注入空密码去连生产比中止危险得多。

    include_secrets=False 时跳过敏感变量（对外 resolved 接口的默认口径，
    避免任何登录用户拉一次接口就把密码全拿走）。

    执行器与对外的 resolved 接口共用本函数，保证外部系统自行拼装 ansible
    调用时与平台内部注入语义完全一致。
    """
    from .models import ExecVariable

    variables = {}
    qs = ExecVariable.objects.filter(
        enabled=True, project_group_id=project_group_id,
        channel_id=channel_id, environment_id=environment_id,
    )
    for var in qs:
        if var.is_secret:
            if not include_secrets:
                continue
            variables[var.key] = var.get_value()
            continue
        raw = var.value or ''
        if not raw.strip():
            variables[var.key] = ''
            continue
        try:
            variables[var.key] = yaml.safe_load(raw)
        except yaml.YAMLError:
            variables[var.key] = raw
    return variables


def collect_dim_secret_values(project_group_id, channel_id, environment_id):
    """该维度下敏感变量的明文集合，供输出与归档打码（见 live.LiveSink.set_secrets）。

    单独一个函数而不是让 collect_dim_variables 多返回一个值：那样所有调用方
    （含对外接口）都要改签名，而只有执行路径需要这份明文。
    解不开的变量跳过——打码用不上它，真正的报错留给注入路径去抛。
    """
    from common.crypto import DecryptError
    from .models import ExecVariable

    values = set()
    qs = ExecVariable.objects.filter(
        enabled=True, is_secret=True, project_group_id=project_group_id,
        channel_id=channel_id, environment_id=environment_id,
    )
    for var in qs:
        try:
            plain = var.get_value()
        except DecryptError:
            continue
        if plain and len(str(plain)) >= SECRET_MASK_MIN_LEN:
            values.add(str(plain))
    return values


def collect_artifact_vars(project_group_id, channel_id, environment_id, locked=''):
    """按维度查询构建制品，返回注入 CD 剧本的变量字典。

    - locked 非空：锁定该版本，不存在则抛 ArtifactLookupError 供调用方 fail fast；
    - 未锁定：取该维度最新制品；无制品返回空 dict（静默跳过，不影响非 CD 模块）。
    """
    from apps.artifact_mgr.models import Artifact

    locked = str(locked or '').strip()
    artifact = Artifact.latest_for(
        project_group_id, channel_id, environment_id, version=locked or None)
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


def _artifact_vars(step):
    """按模块有效维度取制品变量（step.params.artifact_version 可锁定版本）。"""
    pg, ch, env = _effective_dims(step)
    locked = (getattr(step, 'params', None) or {}).get('artifact_version')
    return collect_artifact_vars(pg, ch, env, locked=locked)


def _effective_dims(step):
    """模块生效维度：自定义范围用模块自身维度，否则用任务全局维度。"""
    if getattr(step, 'use_custom_scope', False):
        return step.project_group_id, step.channel_id, step.environment_id
    task = step.task
    return task.project_group_id, task.channel_id, task.environment_id


def step_vars(step):
    """模块生效变量：变量管理（按模块有效维度精确匹配）+ CD 制品变量。

    制品变量后合并，同名覆盖变量管理（与原先两段 -e 的先后语义一致）；
    模块自身的额外变量不在此列，由调用方作为最后一个 -e 传入。
    """
    pg, ch, env = _effective_dims(step)
    variables = collect_dim_variables(pg, ch, env)
    variables.update(_artifact_vars(step))
    return variables


def step_secret_values(step):
    """该模块生效的敏感变量明文集合，供输出与归档打码。

    与 step_vars 同一套维度口径；制品变量不含秘密，不必纳入。
    """
    pg, ch, env = _effective_dims(step)
    return collect_dim_secret_values(pg, ch, env)


def dump_vars_text(variables):
    """变量字典 → YAML 文本（供落盘 -e @file 与 agent 通道下发共用）。"""
    return yaml.safe_dump(variables, allow_unicode=True, default_flow_style=False, sort_keys=True)


def write_vars_file(variables, workdir, name=VARS_FILENAME):
    """在工作目录内落变量文件并收紧权限，返回绝对路径。

    走文件而非命令行 JSON：变量值不进 argv（同机 ps 可见）、不进
    common.ansible 的命令行日志，且事后可原样复制复现。
    """
    path = os.path.join(workdir, name)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(dump_vars_text(variables))
    os.chmod(path, 0o600)
    return path


def extra_vars_args(step, workdir):
    """拼装 --extra-vars 参数：变量管理 + 制品变量落 workdir/vars.yml 以
    `-e @文件` 传入，模块自身的额外变量作为后一个 -e 传入（ansible 后者
    覆盖前者同名项）。

    仍是 extra vars（ansible 最高优先级），仅载体从命令行 JSON 换成文件，
    优先级语义不变：剧本内 vars 不会反过来覆盖平台变量。
    """
    args = []
    variables = step_vars(step)
    if variables:
        args += ['--extra-vars', '@' + write_vars_file(variables, workdir)]
    if (getattr(step, 'extra_vars', '') or '').strip():
        args += ['--extra-vars', step.extra_vars.strip()]
    return args


def _step_servers(step, task):
    """解析模块的执行主机：自定义范围实时匹配，否则用模块自己的勾选快照
    （为空回退任务级快照，兼容存量/外部调用），再按模块服务类型收窄。"""
    selected = list(step.servers.all()) or list(task.servers.all())
    return step.resolve_servers(selected)


def _guard(servers):
    """模块执行前置：主机非空且均已规划服务（兜底执行排队期间规划被撤销）。"""
    if not servers:
        return '未匹配到可执行主机（自定义范围维度无主机、全局范围未勾选，或服务类型过滤后为空）。'
    unplanned = _unplanned_servers(servers)
    if unplanned:
        names = '、'.join(f'{s.name}({s.conn_ip})' for s in unplanned)
        return f'主机 {names} 未规划任何服务类型，不允许执行，请先在「配置管理 → 服务器分配」完成服务规划。'
    return None


def _split_by_agent(servers):
    """按执行通道拆分主机：返回 (直连主机列表, [(agent, 该 agent 主机列表)])。

    通道由 agent_mgr.channel 统一解析（主机三态优先于项目组/环境路由规则），
    此处只做分组。agent 组按 agent id 排序，保证输出顺序稳定。
    """
    from apps.agent_mgr.channel import resolve_channels

    servers = list(servers)
    resolved = resolve_channels(servers)
    direct, groups = [], {}
    for srv in servers:
        agent = resolved[srv.pk][0]
        if agent is None:
            direct.append(srv)
        else:
            groups.setdefault(agent, []).append(srv)
    return direct, sorted(groups.items(), key=lambda item: item[0].pk)


def _run_channels(step, task, servers, run_direct, agent_payload, on_data=None, archive_dir=None):
    """通道分组执行：直连组本地执行，agent 组经 dispatch 下发，输出拼接。

    run_direct(直连主机列表) → (rc, out)；agent_payload(该组主机列表) →
    下发 payload 字典。全部为直连（现状主流）时输出不加通道标头，与旧行为
    完全一致；rc 取首个非零（保留 124 等语义）。on_data 为实时输出回调，
    通道标头也经它喂给实时视图（最终返回文本仍由本函数拼装）。

    agent 组下发前把 payload 内的 inventory/变量归档到 archive_dir/agent-<name>/，
    供事后在 agent 主机上复现（直连组的现场由 run_direct 自行归档）。
    """
    from apps.agent_mgr.dispatch import run_via_agent

    direct, agent_groups = _split_by_agent(servers)
    if not agent_groups:
        return run_direct(direct)

    def emit(text):
        if on_data:
            on_data(text)

    parts, final_rc = [], 0
    if direct:
        header = f'───── [直连 · {len(direct)} 台] ─────'
        emit(header + '\n')
        rc, out = run_direct(direct)
        parts.append(f'{header}\n{out}')
        final_rc = final_rc or rc
    for agent, group in agent_groups:
        header = f'───── [经 Agent {agent.name} · {len(group)} 台] ─────'
        emit(('\n\n' if parts else '') + header + '\n')
        payload = agent_payload(group)
        archive_scene(archive_dir, f'agent-{agent.pk}', payload.get('inventory'),
                      step_vars(step), secrets=step_secret_values(step))
        rc, out = run_via_agent(
            agent, payload, timeout=step.timeout or 300,
            task_id=task.pk, step_order=step.order, on_data=on_data)
        parts.append(f'{header}\n{out}')
        final_rc = final_rc or rc
    return final_rc, '\n\n'.join(parts)


def _run_command_step(step, task, servers, on_data=None, archive_dir=None):
    if not step.command.strip():
        return 1, '命令内容为空。'

    def run_direct(direct_servers):
        workdir = tempfile.mkdtemp(prefix='ops_cmd_')
        try:
            inventory_path = _write_inventory(direct_servers, task.credential, workdir)
            cmd = ['ansible', 'all', '-i', inventory_path] + _forks_args(task) \
                + extra_vars_args(step, workdir) + ['-m', 'shell', '-a', step.command]
            archive_scene(
                archive_dir, 'direct',
                _inventory_content(direct_servers, task.credential, None, include_secrets=False),
                step_vars(step), cmd=cmd, secrets=step_secret_values(step))
            return run_stream(cmd, timeout=step.timeout or 300, on_data=on_data)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def agent_payload(group):
        return _agent_payload_base(step, task, group) | {'command': step.command}

    return _run_channels(step, task, servers, run_direct, agent_payload,
                         on_data=on_data, archive_dir=archive_dir)


def _run_playbook_step(step, task, servers, on_data=None, archive_dir=None):
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

    def run_direct(direct_servers):
        workdir = tempfile.mkdtemp(prefix='ops_pb_')
        try:
            inventory_path = _write_inventory(direct_servers, task.credential, workdir)
            cmd = ['ansible-playbook', playbook_path, '-i', inventory_path] + _forks_args(task)
            if (step.tags or '').strip():
                cmd += ['--tags', step.tags.strip()]
            cmd += extra_vars_args(step, workdir)
            archive_scene(
                archive_dir, 'direct',
                _inventory_content(direct_servers, task.credential, None, include_secrets=False),
                step_vars(step), cmd=cmd, cwd=project_dir, secrets=step_secret_values(step))
            return run_stream(cmd, timeout=step.timeout or 300, cwd=project_dir, on_data=on_data)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def agent_payload(group):
        # 剧本按 agent 侧项目目录内相对路径执行（agent 与 manager 同步同一 git 仓库）
        return _agent_payload_base(step, task, group) | {
            'playbook': step.playbook.strip(),
            'tags': (step.tags or '').strip(),
            'project_sync': True,
        }

    return _run_channels(step, task, servers, run_direct, agent_payload,
                         on_data=on_data, archive_dir=archive_dir)


def _agent_payload_base(step, task, group):
    """agent 下发 payload 公共部分：无密钥 inventory + 变量 + 并发/超时。

    变量双发，兼容新旧 agent（旧版本不认新字段，缺了会静默丢变量）：
      - vars_file/step_extra_vars（新）：变量落 YAML 文本，agent 侧写文件后
        `-e @vars.yml`，再以 step_extra_vars 作为后一个 -e；
      - extra_vars（旧）：按注入顺序排列的 --extra-vars 值列表（变量管理+制品
        合并 JSON → 模块额外变量），旧 agent 逐个以 -e 传入，效果等价。
    新 agent 见到 vars_file 即忽略 extra_vars，两者内容一致不会双重注入。
    """
    variables = step_vars(step)
    module_extra = (getattr(step, 'extra_vars', '') or '').strip()
    legacy = ([json.dumps(variables, ensure_ascii=False)] if variables else []) \
        + ([module_extra] if module_extra else [])
    return {
        'step_type': step.task_type,
        'inventory': _inventory_content(group, task.credential, None, include_secrets=False),
        'vars_file': dump_vars_text(variables) if variables else '',
        'step_extra_vars': module_extra,
        'extra_vars': legacy,
        'forks': 1 if task.exec_mode == 'serial' else (getattr(task, 'forks', None) or 10),
        'timeout': step.timeout or 300,
    }


def _run_method_step(step, task, servers, on_data=None, archive_dir=None):
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
    # v1 限制：方法模块不走 agent 通道。剧本类方法从 manager 直连 SSH，
    # agent 通道主机（NAT 内网）不可达，提前失败给出明确指引。
    if method.provider == 'ansible_playbook':
        from apps.agent_mgr.channel import SOURCE_LABELS, resolve_channels

        resolved = resolve_channels(servers)
        agent_hosts = [(s, *resolved[s.pk]) for s in servers if resolved[s.pk][0]]
        if agent_hosts:
            # 带上通道来源：命中路由规则时，主机上看不到任何 agent 配置
            names = '、'.join(
                f'{s.name}({s.conn_ip} → Agent {agent.name}，{SOURCE_LABELS[source]})'
                for s, agent, source in agent_hosts[:5]
            )
            return 1, (f'方法模块暂不支持经 Agent 通道执行：{names}。'
                       '请改用命令/剧本模块，或把主机执行通道改为「强制直连」。')

    workdir = tempfile.mkdtemp(prefix='ops_mtd_')
    try:
        inventory_path = _write_inventory(servers, task.credential, workdir)
        # 剧本类 provider 的变量与命令由执行器自行拼装（共用 extra_vars_args），
        # 此处仅归档 inventory 与变量；非 ansible provider 无 inventory 语义但归档无害
        archive_scene(
            archive_dir, 'direct',
            _inventory_content(servers, task.credential, None, include_secrets=False),
            step_vars(step), secrets=step_secret_values(step))
        # 执行器按 step 读取 tags/extra_vars/timeout（模块级）与 exec_mode（透传任务级）；
        # servers 供不走 Ansible 的 provider（如 jumpserver）直接消费主机对象；
        # on_data 供走 ansible 的 provider 实时回传输出（其余 provider 忽略）
        return executor.execute(
            step, method, inventory_path=inventory_path, servers=servers, on_data=on_data)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


_STEP_HANDLERS = {
    'command': _run_command_step,
    'playbook': _run_playbook_step,
    'method': _run_method_step,
}

# 执行进度预览上限（live.PREVIEW_LIMIT）；暂停确认轮询间隔（秒）
PROGRESS_PREVIEW_LIMIT = PREVIEW_LIMIT
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


def _persist_progress(sink, extra=None):
    """强制把当前输出（含正在执行模块的实时分片）写入快照与落盘文件。

    extra 为仅供实时查看的临时提示（如失败等待人工处理），不落盘；
    最终完整输出与状态由 tasks.execute_task 统一落库。
    """
    sink.flush(extra=extra, force=True)


def _wait_confirmation(step, task):
    """暂停确认模块：置任务为 paused，轮询确认动作直至继续/中止/超时。

    确认接口（views.confirm）向 pause_action 写 continue/abort，
    本函数消费后清空并把状态恢复为 running。
    """
    from .models import ExecutionTask

    timeout = step.timeout or 300
    ExecutionTask.objects.filter(pk=task.pk).update(
        status='paused', pause_action='', pause_type='confirm')
    push_status(task.pk)  # 推送暂停态，前端切出「继续/中止」操作条
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
        push_status(task.pk)  # 恢复执行：推送状态收起操作条


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
        push_status(task.pk)  # 恢复执行：推送状态收起操作条


def run(task, sink=None):
    """按 order 顺序执行任务流的全部模块，返回 (return_code, output, steps_result)。

    模块失败（rc != 0）后任务流暂停，等待人工处理：重试（重跑当前阶段失败
    模块，可反复）/ 跳过（继续后续模块，任务最终仍记失败）/ 中止（后续模块
    标记 skipped）；等待超时按中止处理。暂停确认模块的中止/超时直接终止
    任务流，不进入失败处理。

    sink 为实时输出汇聚器（LiveSink）：ansible 输出边跑边写库/落盘；未传时
    自建一个仅写库的 sink（直接调用 runner.run 的场景，如测试）。
    """
    steps = list(task.steps.all())
    if not steps:
        return 1, '任务未配置任何模块，请编辑任务后重试。', []

    sink = sink or LiveSink(task.pk)
    outputs, results = [], []
    sink.bind(outputs)
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
        entries = _exec_stage(pairs, task, total, sink, parallel)

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
                _persist_progress(sink, extra=prompt)
                action = _wait_failure_action(task, timeout)
            else:
                action = 'abort_now'
            if action == 'retry':
                attempt = result_by_id[failed_entries[0][0].id]['attempts'] + 1
                outputs.append(f'↻ 已人工确认重试，第 {attempt} 次执行失败模块：{names}。')
                _persist_progress(sink)
                retry_pairs = [(e[0], idx_by_id[e[0].id]) for e in failed_entries]
                entries = _exec_stage(retry_pairs, task, total, sink, parallel, attempt=attempt)
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
        # 每个阶段结束即持久化进度（写库预览 + 补齐落盘）
        _persist_progress(sink)

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


def _exec_stage(pairs, task, total, sink, parallel, attempt=1):
    """执行一个阶段（或阶段内失败模块的重试子集），按传入顺序返回 entries。

    pairs 为 (step, 模块序号) 列表；attempt > 1 表示重试轮次（体现在模块头）。
    """
    if not parallel:
        step, idx = pairs[0]
        return [_exec_step(step, task, idx, total, sink, parallel=False, attempt=attempt)]
    return _exec_parallel_stage(pairs, task, total, sink, attempt)


def _exec_step(step, task, idx, total, sink, parallel, attempt=1):
    """执行单个模块，返回 (step, header, rc, out, duration, server_count)。

    执行期间输出经 sink 实时写库/落盘（模块头先行登记，结束后由调用方把
    完整输出追加进 outputs，sink 丢弃实时块，两者不会重复）。
    """
    started = time.monotonic()
    retry_mark = f' · 重试#{attempt - 1}' if attempt > 1 else ''
    live_key = (step.id, attempt)
    # 敏感变量明文：注册给 sink 过滤实时输出，并在下面对完整输出兜一遍。
    # 两处都要——实时块与模块完整输出走的是不同的持久化路径。
    secrets = set() if step.task_type == 'pause' else step_secret_values(step)
    if secrets:
        sink.add_secrets(secrets)
    if step.task_type == 'pause':
        # 暂停确认模块：不涉及目标主机，等待人工继续/中止
        header = f'═══ 模块 {idx}/{total}：{step.name}（暂停确认 · 等待人工处理）═══'
        prompt = f'⏸ 任务流已暂停，等待人工确认…（超过 {step.timeout or 300}s 未确认将自动中止）'
        sink.begin(live_key, header, order=idx)
        try:
            sink.feed(live_key, prompt)
            _persist_progress(sink)
            rc, out = _wait_confirmation(step, task)
        finally:
            sink.end(live_key)
        server_count = None
    else:
        servers = _step_servers(step, task)
        mark = ' · 并行组' if parallel else ''
        header = (f'═══ 模块 {idx}/{total}：{step.name}'
                  f'（{step.get_task_type_display()} · {step.scope_label} {len(servers)} 台'
                  f'{step.service_type_label}{mark}{retry_mark}）═══')
        error = _guard(servers)
        if error:
            rc, out = 1, error
        else:
            sink.begin(live_key, header, order=idx)
            try:
                rc, out = _STEP_HANDLERS[step.task_type](
                    step, task, servers, sink.writer(live_key),
                    archive_dir=step_archive_dir(sink, step, attempt))
            except ArtifactLookupError as exc:
                rc, out = 1, str(exc)
            finally:
                sink.end(live_key)
        server_count = len(servers)
    duration = round(time.monotonic() - started, 2)
    return step, header, rc, mask_secrets(out, secrets), duration, server_count


def _exec_parallel_stage(pairs, task, total, sink, attempt=1):
    """并行组：组内模块并发执行，等待全部完成后按模块顺序返回结果。

    任一模块失败不打断组内其他模块（各自跑完），失败由调用方汇总处理
    （暂停等待人工重试/跳过/中止）。重试时 pairs 仅含失败模块（序号沿用）。
    """
    from concurrent.futures import ThreadPoolExecutor

    from django.db import connection as dj_connection

    def _worker(step, idx):
        try:
            return _exec_step(step, task, idx, total, sink, parallel=True, attempt=attempt)
        finally:
            dj_connection.close()  # 线程私有连接，退出前释放

    # 并发数封顶（settings.TASK_PARALLEL_MAX）：每个模块都会起一个 ansible 进程、
    # 再按 --forks 派生子进程，不封顶时组一大就把整台机的 CPU 吃光。
    # 超出上限的模块在池里排队，语义不变（仍是「全组跑完再汇总」）。
    limit = max(1, getattr(settings, 'TASK_PARALLEL_MAX', 5) or 1)
    with ThreadPoolExecutor(max_workers=min(len(pairs), limit)) as pool:
        futures = [pool.submit(_worker, step, idx) for step, idx in pairs]
        return [f.result() for f in futures]
