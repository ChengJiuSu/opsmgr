"""JumpServer 资产注册公共工具（直连 + Agent 双通道）。

被 asset_mgr（服务器批量注册接口）与 method_mgr jumpserver 执行器共用，
放在 common 下避免 app 间循环依赖。两条通道：

- **直连**：manager 与 JumpServer 网络可达，本模块直接发 REST，连接信息读
  系统配置（下方键位）；
- **经 Agent**：manager 在公网、JumpServer 在内网时，打成 `step_type='jumpserver'`
  的执行单元下发 agent，由 agent 在内网逐台注册并结构化回传逐台结果。此路
  **不下发任何 JumpServer 凭据**，凭据只在 agent 本地 config.yaml 维护。

统一入口 register_servers_routed：按通道把主机分组，agent 组下发、直连组本地
发 REST，合并为逐台结果。通道解析见 resolve_registration_agent。

连接信息读系统配置（「系统管理 → 系统配置」维护，仅直连路径使用）：
    jumpserver.url          JumpServer 地址，如 http://192.168.1.10
    jumpserver.token        个人 API Token（Authorization: Token <token>）
    jumpserver.org_id       组织 id（可选，多组织时指定，写入 X-JMS-ORG 头）
    jumpserver.platform_id  平台 id（可选，默认 1 = Linux）
    jumpserver.node_id      节点 id（可选，指定资产挂载节点）
    jumpserver.agent        Agent 名（可选，填了则注册统一经该 agent 执行）
"""
import requests

REQUEST_TIMEOUT = 15  # 单次 API 请求超时（秒）
# agent 通道能力位：老 agent 不支持 jumpserver 单元时 fail fast
AGENT_CAPABILITY = 'jumpserver'
# agent 单元超时：基准 + 每台增量，封顶（逐台两次 REST，慢在 JumpServer 侧）
AGENT_TIMEOUT_BASE = 15
AGENT_TIMEOUT_PER_HOST = 3
AGENT_TIMEOUT_MAX = 300
# 单个下发单元的主机数上限（与 agent 侧校验对齐）：超出自动分批，不硬失败
AGENT_MAX_HOSTS = 500


def _settings():
    from apps.system_cfg.services import get_config

    return {
        'url': get_config('jumpserver.url').rstrip('/'),
        'token': get_config('jumpserver.token'),
        'org_id': get_config('jumpserver.org_id'),
        'platform_id': get_config('jumpserver.platform_id') or '1',
        'node_id': get_config('jumpserver.node_id'),
    }


def _session(cfg):
    session = requests.Session()
    # JumpServer 一般在内网，忽略 http_proxy 等环境代理，避免开发机代理劫持导致 502
    session.trust_env = False
    session.headers['Authorization'] = f'Token {cfg["token"]}'
    if cfg['org_id']:
        session.headers['X-JMS-ORG'] = cfg['org_id']
    return session


def register_servers(servers):
    """把平台服务器逐台注册为 JumpServer 主机资产，返回逐台结果列表。

    幂等：按主机名查到已存在资产则跳过不重复创建（平台主机名全局唯一，
    私网 IP 跨网段可重复，故查重以 name 为准）。
    连接地址（address）公网优先：有公网 IP 用公网，无则用私网（NAT 出局主机）。
    返回 [{'id', 'name', 'ip', 'ok', 'msg'}, ...]（ip 为实际注册地址）。
    连接信息未配置时抛 ValueError（调用方转为友好报错）。
    """
    cfg = _settings()
    if not (cfg['url'] and cfg['token']):
        raise ValueError(
            'JumpServer 连接信息不完整，请在「系统配置」维护 jumpserver.url / jumpserver.token。'
        )
    session = _session(cfg)
    return [
        {'id': s.id, 'name': s.name, 'ip': s.conn_ip, **_register_one(session, cfg, s)}
        for s in servers
    ]


def _register_one(session, cfg, server):
    """注册单台主机，返回 {'ok': bool, 'msg': str}。"""
    base = cfg['url']
    try:
        # 先按主机名查重，保证重复执行幂等
        resp = session.get(
            f'{base}/api/v1/assets/hosts/',
            params={'name': server.name}, timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 401:
            return {'ok': False, 'msg': 'JumpServer 认证失败（token 无效或已过期）'}
        if resp.ok:
            data = resp.json()
            existing = data.get('results', data) if isinstance(data, dict) else data
            if existing:
                return {'ok': True, 'msg': '已存在，跳过'}

        payload = {
            'name': server.name,
            'address': server.conn_ip,
            'platform': {'pk': int(cfg['platform_id'])},
            'protocols': [{'name': 'ssh', 'port': server.ssh_port or 22}],
            'is_active': True,
            'comment': server.remark or '',
        }
        if cfg['node_id']:
            payload['nodes'] = [{'pk': cfg['node_id']}]
        resp = session.post(f'{base}/api/v1/assets/hosts/', json=payload, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 400 and 'name' in (resp.text or ''):
            # 兜底：查重与创建之间的并发窗口，或查重接口异常时命中资产名唯一约束
            raise ValueError(
                f'主机「{server.name}」（{server.conn_ip}）与 JumpServer 已有资产重名，'
                '禁止同步，请先修改名称保证唯一。'
            )
        if resp.status_code in (200, 201):
            return {'ok': True, 'msg': '注册成功'}
        return {'ok': False, 'msg': f'注册失败：HTTP {resp.status_code} {(resp.text or "")[:200]}'}
    except requests.RequestException as exc:
        return {'ok': False, 'msg': f'请求异常：{exc}'}


# ── Agent 通道 ────────────────────────────────────────────


def build_host_entries(servers):
    """打包下发给 agent 的主机清单（不含任何凭据）。

    key 用平台服务器 id：回传结果按它对回主机，不依赖主机名。
    address 与直连口径一致（公网优先，见 Server.conn_ip）。
    """
    return [
        {
            'key': str(s.id),
            'name': s.name,
            'address': s.conn_ip,
            'ssh_port': s.ssh_port or 22,
            'comment': s.remark or '',
        }
        for s in servers
    ]


def agent_timeout(host_count):
    """agent 单元超时（秒）：按主机数线性放大并封顶。"""
    return min(AGENT_TIMEOUT_BASE + AGENT_TIMEOUT_PER_HOST * host_count, AGENT_TIMEOUT_MAX)


def register_via_agent(agent, servers, task_id=None, step_order=None):
    """经 agent 注册，返回与 register_servers 同结构的逐台结果列表。

    agent 回传的 host_results 缺哪台（通道失败、重名中止整批、超时），该台
    按失败计，原因取通道输出末行——不静默当成功。
    task_id/step_order 供「Agent 任务单元」回溯来源（资产页调用时为空）。
    """
    servers = list(servers)
    results = []
    # 超过单元上限自动分批下发（agent 侧同样按 500 台校验）
    for start in range(0, len(servers), AGENT_MAX_HOSTS):
        results += _register_via_agent_batch(
            agent, servers[start:start + AGENT_MAX_HOSTS], task_id, step_order)
    return results


def _register_via_agent_batch(agent, servers, task_id, step_order):
    from apps.agent_mgr.dispatch import run_via_agent_detail

    timeout = agent_timeout(len(servers))
    payload = {
        'step_type': 'jumpserver',
        'hosts': build_host_entries(servers),
        'timeout': timeout,
    }
    rc, output, result = run_via_agent_detail(
        agent, payload, timeout=timeout, capability=AGENT_CAPABILITY,
        task_id=task_id, step_order=step_order)
    host_results = (result or {}).get('host_results') or {}
    fallback = _channel_failure_msg(rc, output)
    return [
        {'id': s.id, 'name': s.name, 'ip': s.conn_ip,
         **_agent_host_result(host_results.get(str(s.id)), fallback)}
        for s in servers
    ]


def _agent_host_result(item, fallback):
    if not isinstance(item, dict) or 'ok' not in item:
        return {'ok': False, 'msg': fallback}
    return {'ok': bool(item.get('ok')), 'msg': str(item.get('msg') or '')}


def _channel_failure_msg(rc, output):
    """agent 未回传某台结果时的兜底原因：取输出末行（通道失败即为原因本身）。"""
    lines = [line.strip() for line in (output or '').splitlines() if line.strip()]
    tail = lines[-1][:200] if lines else ''
    if rc == 0:
        return f'agent 未回传该主机结果{"：" + tail if tail else ""}'
    return tail or f'经 Agent 注册失败（rc={rc}）'


# ── 通道解析与分组注册 ────────────────────────────────────


def resolve_registration_agent(extra_candidates=(), project_group_id=None,
                               environment_id=None):
    """解析 JumpServer 注册走哪个 agent，返回 (agent 或 None, source)。

    extra_candidates 为调用方的高优先级候选 [(source, agent 名), ...]（方法
    模块的模块参数 / 方法配置），本函数在其后追加系统配置 jumpserver.agent；
    全空时按任务维度回落 AgentRoute 规则（无维度信息则不回落）。

    返回 (None, SOURCE_DIRECT) 不等于「直连」：register_servers_routed 会再
    按每台主机自身的执行通道分组——内网主机本就有自己的 agent。
    名字写了却查不到 agent 时抛 ChannelError（不静默回退）。
    """
    from apps.agent_mgr.channel import SOURCE_CONFIG, resolve_service_agent
    from apps.system_cfg.services import get_config

    candidates = list(extra_candidates) + [(SOURCE_CONFIG, get_config('jumpserver.agent'))]
    return resolve_service_agent(
        candidates, project_group_id=project_group_id, environment_id=environment_id)


def register_servers_routed(servers, agent=None, source=None, task_id=None,
                            step_order=None):
    """按执行通道分组注册，返回 (逐台结果列表, 通道说明列表)。

    agent 非空 = 调用方已解析出统一通道（系统配置/方法配置/模块参数），全部
    主机走它；为空则按每台主机自身的执行通道（channel.resolve_channels，与
    命令/剧本模块同一口径）分组，未命中 agent 的主机走直连。

    结果顺序与入参一致。通道说明为 [{'agent', 'source', 'count'}, ...]，供
    接口返回与任务日志标头展示。

    直连组连接信息缺失/重名中止（register_servers 抛 ValueError）时：整批都是
    直连则原样抛出（保持「配置没填好」的明确报错），混合通道则转成该组逐台
    失败，不连累已经注册成功的 agent 组。
    """
    from apps.agent_mgr.channel import SOURCE_CONFIG, resolve_channels

    servers = list(servers)
    if not servers:
        return [], []

    if agent is not None:
        # source 只影响展示文案；调用方一般都带着解析出来的来源
        groups = [(agent, source or SOURCE_CONFIG, servers)]
    else:
        resolved = resolve_channels(servers)
        buckets = {}
        for server in servers:
            node, src = resolved[server.pk]
            buckets.setdefault((node.pk if node else None, src), (node, src, []))[2].append(server)
        groups = list(buckets.values())

    all_direct = all(node is None for node, _src, _hosts in groups)
    merged = {}
    channels = []
    for node, src, hosts in groups:
        if node is None:
            merged.update(_register_direct_group(hosts, all_direct))
        else:
            merged.update({
                r['id']: r for r in register_via_agent(
                    node, hosts, task_id=task_id, step_order=step_order)})
        channels.append({
            'agent': node.name if node else '',
            'source': src,
            'count': len(hosts),
        })
    return [merged[s.id] for s in servers], channels


def _register_direct_group(servers, reraise):
    """直连组注册，返回 {server_id: 结果}；reraise=False 时把整批异常转逐台失败。"""
    try:
        return {r['id']: r for r in register_servers(servers)}
    except ValueError as exc:
        if reraise:
            raise
        return {
            s.id: {'id': s.id, 'name': s.name, 'ip': s.conn_ip, 'ok': False, 'msg': str(exc)}
            for s in servers
        }


def channel_summary_lines(channels):
    """通道说明转中文行（任务日志标头 / 接口消息共用）。"""
    from apps.agent_mgr.channel import SOURCE_LABELS

    lines = []
    for item in channels:
        label = SOURCE_LABELS.get(item['source'], item['source'])
        where = f'经 Agent {item["agent"]}（{label}）' if item['agent'] else '直连'
        lines.append(f'[通道] {where}：{item["count"]} 台')
    return lines
