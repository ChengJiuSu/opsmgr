"""执行通道解析：主机三态 + 项目组/环境路由规则 → 实际执行通道。

统一口径（runner 分组、方法模块拦截、证书分发、服务器列表展示共用）：

    Server.channel_mode = 'direct'                  → 直连
    Server.channel_mode = 'agent' 且 agent 有效      → 该 agent（source=host）
    Server.channel_mode = 'auto'（默认）：
        AgentRoute(项目组=主机项目组, 环境=主机环境, enabled)  → 该 agent（source=rule）
        AgentRoute(项目组=主机项目组, 环境为空,      enabled)  → 该 agent（source=rule）
        无命中                                                → 直连

设计取舍：

- 「mode=agent 但 agent 为空」（agent 被删，FK SET_NULL）按 auto 处理，避免
  删 agent 把主机变成不可执行的僵尸配置；
- 规则命中的 agent 不看 AgentNode.enabled：agent 禁用只影响能否建连，下发时
  由 dispatch fail fast 给出明确原因。要让主机回退直连请停用规则本身
  （AgentRoute.enabled=False），而不是禁用 agent——NAT 主机静默改走直连只会
  换来一次 SSH 超时。
"""
from .models import AgentNode, AgentRoute

# 通道来源：host=主机显式指定，rule=命中路由规则，direct=直连
SOURCE_HOST = 'host'
SOURCE_RULE = 'rule'
SOURCE_DIRECT = 'direct'


def _load_rules(project_group_ids):
    """加载相关项目组的启用规则，返回 (精确表, 项目组默认表)。

    精确表键为 (project_group_id, environment_id)，默认表键为 project_group_id。
    一次查库，避免逐主机 N+1。
    """
    exact, default = {}, {}
    if not project_group_ids:
        return exact, default
    rules = (
        AgentRoute.objects
        .filter(enabled=True, project_group_id__in=project_group_ids)
        .select_related('agent')
    )
    for rule in rules:
        if rule.environment_id is None:
            default[rule.project_group_id] = rule.agent
        else:
            exact[(rule.project_group_id, rule.environment_id)] = rule.agent
    return exact, default


def resolve_channels(servers):
    """批量解析：返回 {server_id: (AgentNode 或 None, source)}。

    servers 为 Server 列表/查询集；调用方无需预先 select_related('agent')，
    显式指定的 agent 在此统一批量取回。
    """
    servers = list(servers)
    pinned_ids = {
        s.agent_id for s in servers
        if s.channel_mode == 'agent' and s.agent_id
    }
    pinned = {}
    if pinned_ids:
        # 已随 select_related 带出的实例直接复用，其余一次性补齐
        for srv in servers:
            if srv.agent_id in pinned_ids and 'agent' in srv._state.fields_cache:
                pinned[srv.agent_id] = srv.agent
        missing = pinned_ids - set(pinned)
        if missing:
            pinned.update(AgentNode.objects.in_bulk(missing))

    # 走规则的主机：非 direct、且未钉死到有效 agent（含 mode 取值异常兜底为 auto）
    auto_pgs = {
        srv.project_group_id for srv in servers
        if srv.project_group_id and srv.channel_mode != 'direct'
        and not (srv.channel_mode == 'agent' and srv.agent_id)
    }
    exact, default = _load_rules(auto_pgs)

    resolved = {}
    for srv in servers:
        if srv.channel_mode == 'direct':
            resolved[srv.pk] = (None, SOURCE_DIRECT)
            continue
        if srv.channel_mode == 'agent' and srv.agent_id:
            agent = pinned.get(srv.agent_id)
            resolved[srv.pk] = (agent, SOURCE_HOST) if agent else (None, SOURCE_DIRECT)
            continue
        agent = exact.get((srv.project_group_id, srv.environment_id))
        if agent is None:
            agent = default.get(srv.project_group_id)
        resolved[srv.pk] = (agent, SOURCE_RULE) if agent else (None, SOURCE_DIRECT)
    return resolved


def resolve_channel(server):
    """单主机解析糖：返回 (AgentNode 或 None, source)。"""
    return resolve_channels([server])[server.pk]


def resolve_agent(server):
    """单主机只取 agent（None = 直连）。"""
    return resolve_channel(server)[0]
