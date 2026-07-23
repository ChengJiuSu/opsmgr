"""JumpServer 资产注册公共工具。

被 asset_mgr（服务器批量注册接口）与 method_mgr jumpserver 执行器共用，
放在 common 下避免 app 间循环依赖。

连接信息读系统配置（「系统管理 → 系统配置」维护）：
    jumpserver.url          JumpServer 地址，如 http://192.168.1.10
    jumpserver.token        个人 API Token（Authorization: Token <token>）
    jumpserver.org_id       组织 id（可选，多组织时指定，写入 X-JMS-ORG 头）
    jumpserver.platform_id  平台 id（可选，默认 1 = Linux）
    jumpserver.node_id      节点 id（可选，指定资产挂载节点）
"""
import requests

REQUEST_TIMEOUT = 15  # 单次 API 请求超时（秒）


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
        {'id': s.id, 'name': s.name, 'ip': _address(s), **_register_one(session, cfg, s)}
        for s in servers
    ]


def _address(server):
    """资产连接地址：公网优先，NAT 出局主机（无公网）回退私网。"""
    return server.public_ip or server.private_ip


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
            'address': _address(server),
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
                f'主机「{server.name}」（{_address(server)}）与 JumpServer 已有资产重名，'
                '禁止同步，请先修改名称保证唯一。'
            )
        if resp.status_code in (200, 201):
            return {'ok': True, 'msg': '注册成功'}
        return {'ok': False, 'msg': f'注册失败：HTTP {resp.status_code} {(resp.text or "")[:200]}'}
    except requests.RequestException as exc:
        return {'ok': False, 'msg': f'请求异常：{exc}'}
