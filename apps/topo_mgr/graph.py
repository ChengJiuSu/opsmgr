"""整图取数、整图保存与自动编排。

三件事都放这里而不是散在视图里，理由是它们共用同一套「引用型节点怎么解析成实时
信息」的口径（`resolve_refs`）：读图要它，保存时校验引用是否存在要它，自动编排
生成的节点标题也要它。分开写必然分叉，分叉的表现是「图上写的名字和详情页不一样」。

## 保存为什么是整图覆盖

前端画布的状态是「一堆节点 + 一堆连线」，用户一次拖拽可能同时改十个节点的坐标、
删两条线、加一个节点。拆成 N 个 REST 子接口意味着前端要自己算 diff、要处理「第 7
个请求失败了前 6 个已经落库」的半保存状态。整图 PUT 一次事务落库，幂等，重放安全。

代价是并发覆盖：两人同时编辑同一张图，后保存的赢。10 人内部平台不值得为此上乐观
锁——真要防，加一个 `updated_at` 版本号比较即可，字段已经有了。
"""

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.asset_mgr.models import Server
from apps.cloud_mgr.models import CertDeployment, Certificate, DnsRecord, Domain

from .models import ALL_KINDS, EXTERNAL_KINDS, REF_KINDS, TopoEdge, TopoNode

#: 自动编排的分层：kind → 列号。图从左到右就是一条请求链路
#: （域名 → 解析 → 加速/负载均衡 → 主机），所以列号即「离用户多远」。
LAYER_BY_KIND = {
    'domain': 0,
    'accel_domain': 0,
    'dns_record': 1,
    'cdn': 2, 'waf': 2,
    'aws_ga': 2, 'aws_cloudfront': 2, 'gcp_cdn': 2,
    'tencent_cdn': 2, 'aliyun_cdn': 2,
    'aws_alb': 3, 'aws_nlb': 3, 'gcp_lb': 3,
    'tencent_clb': 3, 'aliyun_slb': 3,
    'server': 4,
    'certificate': 5,
    'custom': 3,
}
COLUMN_WIDTH = 280
ROW_HEIGHT = 110


# ──────────────────────────────────────────────────────────
# 引用型节点：实时解析
# ──────────────────────────────────────────────────────────

#: 健康度按「最坏一条」定：一台机同时维护中 + Agent 离线，看图的人该先看到红的
HEALTH_ORDER = ('healthy', 'warning', 'danger')
#: 证书剩余天数进入警告的阈值。acme 续签窗口 30 天，早于它告警只会天天黄
#: （与概览页的 CERT_EXPIRY_WARN_DAYS 刻意不同：那是「该续了」，这里是「快断了」）
CERT_WARN_DAYS = 15


def _worst(issues):
    """一组问题里最严重的级别；没有问题就是健康。"""
    level = 'healthy'
    for item in issues:
        if HEALTH_ORDER.index(item['level']) > HEALTH_ORDER.index(level):
            level = item['level']
    return level


def _issue(level, text):
    return {'level': level, 'text': text}


def _server_issues(obj):
    """主机的问题清单。

    执行通道也算健康度的一部分：一台 Agent 离线的主机在图上看着好好的，实际
    什么命令都下不去——那正是故障当下最需要当场看见的一件事。
    """
    issues = []
    if obj.status == 'stopped':
        issues.append(_issue('danger', '主机已停止'))
    elif obj.status == 'maintenance':
        issues.append(_issue('warning', '主机维护中'))
    if obj.channel_mode == 'agent':
        agent = obj.agent
        if agent is None:
            issues.append(_issue('danger', '执行通道指定了 Agent，但没有绑定'))
        elif agent.is_offline:  # status + 心跳新鲜度，见 AgentNode.is_offline
            issues.append(_issue('danger', f'执行通道 Agent「{agent.name}」离线'))
    return issues


def _server_info(obj):
    issues = _server_issues(obj)
    return {
        'label': obj.name,
        'sub_label': obj.conn_ip,
        'status': obj.status,
        'status_label': obj.get_status_display(),
        # 状态语义（healthy/warning/danger）由后端给，前端不必按 kind 各写一套
        # 映射；同一个 status 在两个页面染成两种颜色是最容易出的低级不一致
        'health': _worst(issues),
        'issues': issues,
        'link': {'path': '/assets/servers', 'query': {'keyword': obj.name}},
        'extra': {
            'private_ip': obj.private_ip,
            'public_ip': obj.public_ip or '',
            'region': obj.region,
            'cloud_provider': obj.cloud_provider.name if obj.cloud_provider else '',
            'cloud_provider_code': obj.cloud_provider.code if obj.cloud_provider else '',
            'services': [
                f'{s.service_type.name}×{s.process_count}'
                for s in obj.services.all() if s.status == 'deployed'
            ],
        },
    }


def _domain_issues(obj):
    if obj.status == 'expiring':
        return [_issue('warning', '域名即将到期')]
    if obj.status == 'expired':
        return [_issue('danger', '域名已过期')]
    return []


def _domain_info(obj):
    issues = _domain_issues(obj)
    return {
        'label': obj.name,
        'sub_label': obj.cloud_account.name if obj.cloud_account else '',
        'status': obj.status,
        'status_label': obj.get_status_display(),
        'health': _worst(issues),
        'issues': issues,
        'link': {'path': '/cloud/domains', 'query': {'keyword': obj.name}},
        'extra': {'record_count': obj.records.count()},
    }


def _dns_record_issues(obj):
    issues = []
    if obj.sync_status == 'failed':
        issues.append(_issue('danger', '解析记录同步失败，厂商侧可能与本地不一致'))
    elif obj.sync_status == 'pending':
        issues.append(_issue('warning', '解析记录尚未同步到厂商'))
    if not obj.enabled:
        # 图上看着通、实际这条解析是关的：不标出来就会照着图排查半天
        issues.append(_issue('warning', '解析记录已停用'))
    return issues


def _dns_record_info(obj):
    issues = _dns_record_issues(obj)
    return {
        'label': obj.fqdn,
        'sub_label': f'{obj.type} → {obj.value}',
        'status': obj.sync_status,
        'status_label': obj.get_sync_status_display(),
        'health': _worst(issues),
        'issues': issues,
        'link': {'path': '/cloud/domains', 'query': {'keyword': obj.domain.name}},
        'extra': {'type': obj.type, 'value': obj.value, 'line': obj.line,
                  'enabled': obj.enabled},
    }


def _certificate_issues(obj, days):
    if obj.status == 'issuing' and obj.issue_stuck:
        return [_issue('danger', '证书申请卡住（issuing 已超时）')]
    if obj.status in ('failed', 'expired'):
        return [_issue('danger', f'证书{obj.get_status_display()}')]
    if obj.status in ('pending', 'issuing'):
        return [_issue('warning', f'证书{obj.get_status_display()}')]
    if obj.status == 'issued' and days is not None:
        if days <= 0:
            return [_issue('danger', '证书已过期')]
        if days <= CERT_WARN_DAYS:
            return [_issue('warning', f'证书 {days} 天后过期')]
    return []


def _certificate_info(obj):
    days = (obj.expires_at - timezone.now()).days if obj.expires_at else None
    issues = _certificate_issues(obj, days)
    return {
        'label': obj.name,
        'sub_label': f'剩余 {days} 天' if days is not None else '',
        'status': obj.status,
        'status_label': obj.get_status_display(),
        'health': _worst(issues),
        'issues': issues,
        'link': {'path': '/cloud/certificates', 'query': {'keyword': obj.name}},
        'extra': {'domains': obj.domains, 'expires_at': obj.expires_at,
                  'auto_renew': obj.auto_renew},
    }


#: kind → (查询集构造, 信息提取)。加一种引用型资源只改这张表
REF_SOURCES = {
    'server': (
        lambda ids: Server.objects.filter(id__in=ids)
        .select_related('cloud_provider', 'agent')
        .prefetch_related('services__service_type'),
        _server_info,
    ),
    'domain': (
        lambda ids: Domain.objects.filter(id__in=ids).select_related('cloud_account'),
        _domain_info,
    ),
    'dns_record': (
        lambda ids: DnsRecord.objects.filter(id__in=ids).select_related('domain'),
        _dns_record_info,
    ),
    'certificate': (
        lambda ids: Certificate.objects.filter(id__in=ids),
        _certificate_info,
    ),
}


def resolve_refs(nodes):
    """批量解析引用型节点 → {(kind, ref_id): info}。

    按 kind 分组批查，不逐节点查库：一张五十个节点的图否则就是五十次查询。
    """
    grouped = {}
    for node in nodes:
        if node.source == 'ref' and node.ref_id:
            grouped.setdefault(node.kind, set()).add(node.ref_id)
    resolved = {}
    for kind, ids in grouped.items():
        source = REF_SOURCES.get(kind)
        if source is None:
            continue
        queryset, extract = source
        for obj in queryset(ids):
            info = extract(obj)
            info['exists'] = True
            resolved[(kind, obj.id)] = info
    return resolved


def node_payload(node, resolved):
    """单个节点的响应体。引用型节点的展示名以实时值为准，失联则回落到存的 label。"""
    data = {
        'id': node.id,
        'source': node.source,
        'kind': node.kind,
        'kind_label': node.kind_label,
        'ref_id': node.ref_id,
        'label': node.label,
        'sub_label': node.sub_label,
        'attrs': node.attrs or {},
        'x': node.x,
        'y': node.y,
        'pinned': node.pinned,
        'remark': node.remark,
        'ref': None,
    }
    if node.source != 'ref':
        return data
    info = resolved.get((node.kind, node.ref_id))
    if info is None:
        # 实体被删了：节点留在图上并标红。静默移除节点会让链路凭空断开，
        # 而看图的人根本不知道这里原来有东西
        message = f'引用的{REF_KINDS.get(node.kind, node.kind)}已不存在'
        data['ref'] = {'exists': False, 'health': 'danger',
                       'status_label': '已失联',
                       'issues': [{'level': 'danger', 'text': message}],
                       'message': message}
        return data
    data['ref'] = info
    data['label'] = info['label']
    data['sub_label'] = info.get('sub_label') or node.sub_label
    return data


def build_graph(canvas):
    """整图取数：画布 + 节点（含实时引用信息）+ 连线。"""
    nodes = list(canvas.nodes.all())
    resolved = resolve_refs(nodes)
    return {
        'nodes': [node_payload(n, resolved) for n in nodes],
        'edges': [
            {
                'id': e.id,
                'source': e.source_id,
                'target': e.target_id,
                'relation': e.relation,
                'relation_label': e.get_relation_display(),
                'label': e.label,
                'attrs': e.attrs or {},
            }
            for e in canvas.edges.all()
        ],
    }


# ──────────────────────────────────────────────────────────
# 整图保存
# ──────────────────────────────────────────────────────────

class GraphError(Exception):
    """整图保存的校验失败，视图转成 400 + 中文提示。"""


def _clean_node_input(item, index):
    """逐字段校验一个节点入参，返回 (key, 字段字典)。"""
    where = f'第 {index + 1} 个节点'
    key = item.get('key')
    if key in (None, ''):
        raise GraphError(f'{where}缺少 key（前端需为每个节点提供稳定的 key 以关联连线）')
    key = str(key)
    source = item.get('source') or 'external'
    if source not in ('ref', 'external'):
        raise GraphError(f'{where}的 source 非法：{source}')
    kind = item.get('kind') or ''
    if kind not in ALL_KINDS:
        raise GraphError(f'{where}的类型「{kind}」未登记')
    ref_id = item.get('ref_id')
    if source == 'ref':
        if kind not in REF_KINDS:
            raise GraphError(f'{where}：「{ALL_KINDS[kind]}」不是可引用的平台资源')
        if not ref_id:
            raise GraphError(f'{where}：引用平台资源必须带 ref_id')
        ref_id = int(ref_id)
    else:
        if kind not in EXTERNAL_KINDS:
            raise GraphError(f'{where}：「{ALL_KINDS[kind]}」只能引用平台资源，不能手工录入')
        ref_id = None
    label = (item.get('label') or '').strip()
    if not label:
        raise GraphError(f'{where}的显示名称不能为空')
    attrs = item.get('attrs') or {}
    if not isinstance(attrs, dict):
        raise GraphError(f'{where}的 attrs 必须是对象')
    try:
        x, y = float(item.get('x') or 0), float(item.get('y') or 0)
    except (TypeError, ValueError):
        raise GraphError(f'{where}的坐标必须是数字') from None
    return key, {
        'source': source, 'kind': kind, 'ref_id': ref_id,
        'label': label[:200], 'sub_label': (item.get('sub_label') or '')[:200],
        'attrs': attrs, 'x': x, 'y': y,
        'pinned': bool(item.get('pinned')),
        'remark': (item.get('remark') or '')[:255],
        'id': item.get('id'),
    }


def dims_ok(kind, obj, canvas):
    """该实体是否与画布维度相容。

    只有主机有维度可判（项目组/渠道/环境，画布填了地域则还要对上地域），域名 / 解析
    记录 / 证书身上没有维度字段，一律放行——按名字猜它属于哪个项目组，猜错的后果是
    「图上凭空少一个域名」，比不判更糟。

    这条规则在三处共用：整图保存、导入、克隆。少判一处的表现就是「dev 画布上画着
    生产机器」，而那种图比没有图更危险，看图的人会照着它操作。
    """
    if kind != 'server':
        return True
    if (obj.project_group_id != canvas.project_group_id
            or obj.channel_id != canvas.channel_id
            or obj.environment_id != canvas.environment_id):
        return False
    return not canvas.region or obj.region == canvas.region


def _check_refs_exist(prepared, canvas):
    """引用的实体必须真的在库里、且与画布维度相容。

    允许存一个指向不存在实体的节点等于允许造死链；允许挂维度不符的主机等于允许把
    生产机器画到测试画布上。
    """
    wanted = {}
    for fields in prepared.values():
        if fields['source'] == 'ref':
            wanted.setdefault(fields['kind'], set()).add(fields['ref_id'])
    for kind, ids in wanted.items():
        queryset, _ = REF_SOURCES[kind]
        found = {obj.id: obj for obj in queryset(ids)}
        missing = sorted(ids - set(found))
        if missing:
            raise GraphError(
                f'引用的{REF_KINDS[kind]}不存在：ID {missing}')
        mismatched = [obj for obj in found.values() if not dims_ok(kind, obj, canvas)]
        if mismatched:
            names = '、'.join(getattr(o, 'name', str(o.id)) for o in mismatched[:5])
            raise GraphError(
                f'这些{REF_KINDS[kind]}不属于本画布的维度（{canvas.dim_label}）：{names}')


@transaction.atomic
def save_graph(canvas, payload, user=None):
    """整图覆盖保存，返回落库统计。

    节点按 `key` 关联连线；带 `id` 且属于本画布的节点做更新（保住数据库主键，
    影响面反查与审计里引用的就是它），其余新建。不在入参里的节点连同其连线一并删除。
    连线整体重建——它们没有对外身份，重建比算 diff 简单且不会漏。
    """
    nodes_in = payload.get('nodes')
    edges_in = payload.get('edges') or []
    if not isinstance(nodes_in, list) or not isinstance(edges_in, list):
        raise GraphError('nodes 与 edges 必须是数组')

    prepared = {}
    for index, item in enumerate(nodes_in):
        if not isinstance(item, dict):
            raise GraphError(f'第 {index + 1} 个节点不是对象')
        key, fields = _clean_node_input(item, index)
        if key in prepared:
            raise GraphError(f'节点 key 重复：{key}')
        prepared[key] = fields

    # 同一实体在一张图里只能出现一次：数据库有唯一约束，但先在这里报中文错，
    # 否则用户看到的是一条 IntegrityError
    seen_refs = {}
    for key, fields in prepared.items():
        if fields['source'] != 'ref':
            continue
        ref = (fields['kind'], fields['ref_id'])
        if ref in seen_refs:
            raise GraphError(
                f'同一张画布重复引用了{REF_KINDS[fields["kind"]]}「{fields["label"]}」')
        seen_refs[ref] = key
    _check_refs_exist(prepared, canvas)

    existing = {n.id: n for n in canvas.nodes.all()}
    # **先删后建**：入参里没带 id 的节点可能引用与「即将被删的老节点」相同的实体
    # （导入覆盖走的正是这条路——它不知道对面的主键），后删就会先撞上
    # (canvas, kind, ref_id) 唯一约束，报一条 IntegrityError 出去
    declared = {int(f['id']) for f in prepared.values()
                if str(f.get('id') or '').isdigit()}
    removed = [n for nid, n in existing.items() if nid not in declared]
    for node in removed:
        node.delete()  # 级联带走挂在它上面的连线
        existing.pop(node.id, None)

    key_to_node = {}
    for key, fields in prepared.items():
        node_id = fields.pop('id', None)
        node = existing.get(int(node_id)) if str(node_id or '').isdigit() else None
        if node is not None:
            for field, value in fields.items():
                setattr(node, field, value)
        else:
            node = TopoNode(canvas=canvas, **fields)
        node.full_clean(exclude=['canvas'])
        node.save()
        key_to_node[key] = node

    # 连线整体重建
    canvas.edges.all().delete()
    edges = []
    seen_edges = set()
    for index, item in enumerate(edges_in):
        where = f'第 {index + 1} 条连线'
        if not isinstance(item, dict):
            raise GraphError(f'{where}不是对象')
        src_key, dst_key = str(item.get('source') or ''), str(item.get('target') or '')
        if src_key not in key_to_node or dst_key not in key_to_node:
            raise GraphError(f'{where}的起点或终点不在本次提交的节点里')
        if src_key == dst_key:
            raise GraphError(f'{where}的起点与终点相同')
        relation = item.get('relation') or 'custom'
        if relation not in dict(TopoEdge._meta.get_field('relation').choices):
            raise GraphError(f'{where}的关系「{relation}」未登记')
        dedupe = (src_key, dst_key, relation)
        if dedupe in seen_edges:
            raise GraphError(f'{where}与前面的连线完全重复')
        seen_edges.add(dedupe)
        attrs = item.get('attrs') or {}
        if not isinstance(attrs, dict):
            raise GraphError(f'{where}的 attrs 必须是对象')
        edges.append(TopoEdge(
            canvas=canvas,
            source=key_to_node[src_key], target=key_to_node[dst_key],
            relation=relation, label=(item.get('label') or '')[:50], attrs=attrs,
        ))
    TopoEdge.objects.bulk_create(edges)

    layout_mode = payload.get('layout_mode')
    update_fields = ['updated_at']
    if layout_mode in dict(canvas.LAYOUT_CHOICES):
        canvas.layout_mode = layout_mode
        update_fields.append('layout_mode')
    canvas.save(update_fields=update_fields)  # updated_at 由 auto_now 刷新

    return {'nodes': len(key_to_node), 'edges': len(edges), 'removed_nodes': len(removed)}


# ──────────────────────────────────────────────────────────
# 自动编排
# ──────────────────────────────────────────────────────────

def candidate_servers(canvas):
    """画布维度下的主机。region 留空则不按地域收窄（跨地域画布）。"""
    qs = Server.objects.filter(
        project_group_id=canvas.project_group_id,
        channel_id=canvas.channel_id,
        environment_id=canvas.environment_id,
    ).select_related('cloud_provider').prefetch_related('services__service_type')
    if canvas.region:
        qs = qs.filter(region=canvas.region)
    return qs.order_by('name')


def candidates(canvas, kind, keyword=''):
    """左侧「从平台选资源」的候选列表。

    主机按画布维度收窄（这是画布维度的意义所在）；域名 / 解析 / 证书不按维度过滤
    ——它们身上没有项目组字段，硬按名字猜项目会把「猜错」变成「看不见」。
    """
    if kind == 'server':
        qs = candidate_servers(canvas)
        if keyword:
            qs = qs.filter(name__icontains=keyword)
        return [{'ref_id': s.id, 'label': s.name, 'sub_label': s.conn_ip,
                 'extra': s.cloud_provider.name if s.cloud_provider else ''}
                for s in qs[:200]]
    if kind == 'domain':
        qs = Domain.objects.all()
        if keyword:
            qs = qs.filter(name__icontains=keyword)
        return [{'ref_id': d.id, 'label': d.name, 'sub_label': d.get_status_display(),
                 'extra': ''} for d in qs.order_by('name')[:200]]
    if kind == 'dns_record':
        qs = DnsRecord.objects.select_related('domain')
        if keyword:
            qs = qs.filter(Q(rr__icontains=keyword) | Q(domain__name__icontains=keyword))
        return [{'ref_id': r.id, 'label': r.fqdn, 'sub_label': f'{r.type} → {r.value}',
                 'extra': r.get_sync_status_display()}
                for r in qs.order_by('domain__name', 'rr')[:200]]
    if kind == 'certificate':
        qs = Certificate.objects.all()
        if keyword:
            qs = qs.filter(name__icontains=keyword)
        return [{'ref_id': c.id, 'label': c.name, 'sub_label': c.get_status_display(),
                 'extra': ', '.join(c.domains or [])[:60]}
                for c in qs.order_by('name')[:200]]
    raise GraphError(f'「{kind}」不是可引用的平台资源类型')


def _place(kind, taken):
    """给新节点找一个不压别人的位置：按 kind 分列，列内自上而下排。"""
    column = LAYER_BY_KIND.get(kind, 3)
    row = taken.get(column, 0)
    taken[column] = row + 1
    return column * COLUMN_WIDTH, row * ROW_HEIGHT


def _cert_matches(cert_domains, domain_name):
    """证书 SAN 是否覆盖该主域名（含通配符）。"""
    for item in cert_domains or []:
        item = str(item).lower().lstrip('*.').lstrip('.')
        if item and (item == domain_name or domain_name.endswith('.' + item)):
            return True
    return False


@transaction.atomic
def autofill(canvas):
    """按维度自动拉平台已有资源，生成节点与关系。幂等：已在图上的跳过。

    编排规则（只连能确定的关系，猜不出来的留给人手动画）：

    - 该维度下的主机 → 主机节点
    - 解析记录绑到这些主机（DnsRecord.server）→ 解析记录节点 + 解析记录 --解析--> 主机
    - 上述解析记录的主域名 → 主域名节点 + 主域名 --解析--> 解析记录
    - 证书 SAN 覆盖这些主域名 → 证书节点 + 证书 --关联--> 主域名
    - 证书分发目标命中这些主机 → 证书 --落地--> 主机

    GA / 负载均衡这类平台没建模的中间层，由人手动插在解析与主机之间。
    """
    existing_refs = {
        (n.kind, n.ref_id): n
        for n in canvas.nodes.all() if n.source == 'ref' and n.ref_id
    }
    taken = {}
    for node in canvas.nodes.all():
        column = LAYER_BY_KIND.get(node.kind, 3)
        taken[column] = max(taken.get(column, 0), int(node.y // ROW_HEIGHT) + 1)

    created_nodes = []

    def ensure_node(kind, obj):
        key = (kind, obj.id)
        if key in existing_refs:
            return existing_refs[key]
        _, extract = REF_SOURCES[kind]
        info = extract(obj)
        x, y = _place(kind, taken)
        node = TopoNode(canvas=canvas, source='ref', kind=kind, ref_id=obj.id,
                        label=info['label'][:200],
                        sub_label=(info.get('sub_label') or '')[:200], x=x, y=y)
        node.full_clean(exclude=['canvas'])
        node.save()
        existing_refs[key] = node
        created_nodes.append(node)
        return node

    existing_edges = {
        (e.source_id, e.target_id, e.relation) for e in canvas.edges.all()
    }
    new_edges = []

    def ensure_edge(src, dst, relation):
        key = (src.id, dst.id, relation)
        if src.id == dst.id or key in existing_edges:
            return
        existing_edges.add(key)
        new_edges.append(TopoEdge(canvas=canvas, source=src, target=dst,
                                  relation=relation))

    servers = list(candidate_servers(canvas))
    server_nodes = {s.id: ensure_node('server', s) for s in servers}
    if not servers:
        return {'added_nodes': 0, 'added_edges': 0, 'servers': 0}

    records = (DnsRecord.objects
               .filter(server_id__in=server_nodes, enabled=True)
               .select_related('domain'))
    domains = {}
    for record in records:
        record_node = ensure_node('dns_record', record)
        ensure_edge(record_node, server_nodes[record.server_id], 'resolve')
        domain_node = domains.get(record.domain_id) or ensure_node('domain', record.domain)
        domains[record.domain_id] = domain_node
        ensure_edge(domain_node, record_node, 'resolve')

    domain_names = {
        d.id: d.name.lower()
        for d in Domain.objects.filter(id__in=domains)
    }
    if domain_names:
        for cert in Certificate.objects.filter(status='issued'):
            hit = [did for did, name in domain_names.items()
                   if _cert_matches(cert.domains, name)]
            if not hit:
                continue
            cert_node = ensure_node('certificate', cert)
            for did in hit:
                ensure_edge(cert_node, domains[did], 'custom')

    deployments = (CertDeployment.objects
                   .filter(server_id__in=server_nodes, target_type='server')
                   .select_related('certificate'))
    for deployment in deployments:
        cert_node = ensure_node('certificate', deployment.certificate)
        ensure_edge(cert_node, server_nodes[deployment.server_id], 'landing')

    TopoEdge.objects.bulk_create(new_edges)
    canvas.save(update_fields=['updated_at'])
    return {
        'added_nodes': len(created_nodes),
        'added_edges': len(new_edges),
        'servers': len(servers),
    }
