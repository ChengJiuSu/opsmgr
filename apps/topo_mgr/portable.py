"""画布的搬运：导出 JSON、导入 JSON、按维度克隆。

三件事同源，所以放一处：它们都要把「引用型节点指向哪个实体」表达成**自然键**而不是
主键。主键只在本库里成立——导出的图带到另一台实例、或者克隆到另一组维度，`ref_id=17`
毫无意义，`{"name": "aws-usw-p1105-01"}` 才有。

克隆 = 导出后立刻导入（带维度覆盖），不另写一套复制逻辑：两套逻辑必然在「引用型节点
怎么处理」上分叉，而这正是最容易出错的一处。

## 引用型节点必须与目标画布的维度相容

主机身上有项目组/渠道/环境/地域，导入或克隆时必须与目标画布对得上，对不上就**跳过并
报告**。这是本模块最重要的一条规则：把 master 环境的图克隆到 dev，若把生产主机一起
搬过去，得到的是一张「dev 画布上画着生产机器」的图——它比没有图更危险，因为看图的人
会照着它操作。

跳过而不是报错整单失败：真正想复用的是 GA / 负载均衡那层骨架（外部节点与连线），主机
本来就该在目标环境重新挑。报告里逐条列出跳过了什么，让人知道要补哪些。

域名 / 解析记录 / 证书身上没有维度字段，不做维度判定——按名字猜它属于哪个项目组，猜
错的后果是「图上凭空少了一个域名」，比不判更糟。
"""

from django.db import transaction
from django.utils import timezone

from apps.asset_mgr.models import Channel, Environment, ProjectGroup, Server
from apps.cloud_mgr.models import Certificate, DnsRecord, Domain

from . import graph
from .graph import GraphError, dims_ok
from .models import TopoCanvas

class ScopeDenied(GraphError):
    """目标维度不在调用方的数据范围内。

    单独一个类型是为了让视图能回 403 而不是 400：「你没权限写这里」和「你的 JSON
    有问题」对调用方是两件完全不同的事。
    """


#: 导出格式版本。加字段不改版本（导入端按缺省值兼容）；改字段语义才加，
#: 那时导入端要能同时认旧版——否则同事手里导出的老文件全部作废
EXPORT_VERSION = 1


# ──────────────────────────────────────────────────────────
# 自然键
# ──────────────────────────────────────────────────────────

def _server_key(obj):
    return {'name': obj.name}


def _domain_key(obj):
    return {'name': obj.name}


def _dns_record_key(obj):
    # 与 DnsRecord 的唯一约束同字段（域名+主机记录+类型+值+线路），少一个就可能撞上
    # 同名不同线路的两条记录
    return {'domain': obj.domain.name, 'rr': obj.rr, 'type': obj.type,
            'value': obj.value, 'line': obj.line or 'default'}


def _certificate_key(obj):
    return {'name': obj.name}


def _resolve_server(key):
    return Server.objects.filter(name=key.get('name')).first()


def _resolve_domain(key):
    return Domain.objects.filter(name=key.get('name')).first()


def _resolve_dns_record(key):
    return DnsRecord.objects.filter(
        domain__name=key.get('domain'), rr=key.get('rr'), type=key.get('type'),
        value=key.get('value'), line=key.get('line') or 'default',
    ).first()


def _resolve_certificate(key):
    return Certificate.objects.filter(name=key.get('name')).first()


#: kind → (取自然键, 按自然键找回实体)
NATURAL_KEYS = {
    'server': (_server_key, _resolve_server),
    'domain': (_domain_key, _resolve_domain),
    'dns_record': (_dns_record_key, _resolve_dns_record),
    'certificate': (_certificate_key, _resolve_certificate),
}


def natural_key(kind, obj):
    build, _ = NATURAL_KEYS[kind]
    return build(obj)


# ──────────────────────────────────────────────────────────
# 导出
# ──────────────────────────────────────────────────────────

def export_graph(canvas):
    """整图导出为可跨实例搬运的 JSON。

    维度用字典 `code` 而不是 id：换一台实例，id 一定不同，code 是人定的、通常一致。
    """
    nodes = list(canvas.nodes.all())
    refs = {}
    for kind in {n.kind for n in nodes if n.source == 'ref'}:
        source = graph.REF_SOURCES.get(kind)
        if source is None:
            continue
        queryset, _ = source
        ids = [n.ref_id for n in nodes if n.kind == kind and n.source == 'ref']
        refs.update({(kind, obj.id): obj for obj in queryset(ids)})

    node_items = []
    for node in nodes:
        item = {
            'key': f'n{node.id}',
            'source': node.source,
            'kind': node.kind,
            'label': node.label,
            'sub_label': node.sub_label,
            'attrs': node.attrs or {},
            'x': node.x,
            'y': node.y,
            'pinned': node.pinned,
            'remark': node.remark,
            'ref': None,
        }
        if node.source == 'ref':
            obj = refs.get((node.kind, node.ref_id))
            # 失联节点照样导出（ref=null + 存的 label），导入端会当作「解析不到」
            # 跳过并报告。悄悄丢掉它等于把「这里原来有东西」这条信息也一起丢了
            item['ref'] = natural_key(node.kind, obj) if obj is not None else None
        node_items.append(item)

    node_keys = {n.id: f'n{n.id}' for n in nodes}
    return {
        'version': EXPORT_VERSION,
        'exported_at': timezone.now().isoformat(),
        'canvas': {
            'name': canvas.name,
            'slug': canvas.slug,
            'description': canvas.description,
            'region': canvas.region,
            'layout_mode': canvas.layout_mode,
            'project_group': canvas.project_group.code,
            'channel': canvas.channel.code,
            'environment': canvas.environment.code,
        },
        'nodes': node_items,
        'edges': [
            {'source': node_keys[e.source_id], 'target': node_keys[e.target_id],
             'relation': e.relation, 'label': e.label, 'attrs': e.attrs or {},
             'source_handle': e.source_handle, 'target_handle': e.target_handle}
            for e in canvas.edges.all()
        ],
    }


# ──────────────────────────────────────────────────────────
# 导入
# ──────────────────────────────────────────────────────────

def _dict_by_code(model, code, label):
    obj = model.objects.filter(code=code).first()
    if obj is None:
        raise GraphError(f'本实例没有{label}「{code}」，请先在基础字典里建好再导入')
    return obj


def _resolve_target_canvas_fields(payload, overrides):
    """目标画布的字段：导出文件里的值为底，overrides 覆盖。

    overrides 的维度是 id（页面上选的），文件里的是 code（跨实例搬运用）。
    """
    meta = payload.get('canvas') or {}
    overrides = overrides or {}

    def dim(field, model, label):
        if overrides.get(field):
            obj = model.objects.filter(pk=overrides[field]).first()
            if obj is None:
                raise GraphError(f'指定的{label}不存在')
            return obj
        code = meta.get(field)
        if not code:
            raise GraphError(f'导入内容缺少{label}，且未在表单里指定')
        return _dict_by_code(model, code, label)

    name = (overrides.get('name') or meta.get('name') or '').strip()
    slug = (overrides.get('slug') or meta.get('slug') or '').strip().lower()
    if not name or not slug:
        raise GraphError('导入内容缺少画布名称或标识，且未在表单里指定')
    region = overrides.get('region')
    if region is None:
        region = meta.get('region') or ''
    return {
        'name': name[:100],
        'slug': slug[:50],
        'description': (overrides.get('description') or meta.get('description') or '')[:2000],
        'region': str(region).strip()[:50],
        'project_group': dim('project_group', ProjectGroup, '项目组'),
        'channel': dim('channel', Channel, '渠道'),
        'environment': dim('environment', Environment, '环境'),
    }


@transaction.atomic
def import_graph(payload, overrides=None, user=None, overwrite=False, guard=None):
    """把导出的 JSON 落成一张画布，返回 (画布, 报告)。

    同维度同标识的画布已存在时：`overwrite=False` 直接拒绝（默认，免得手一抖把
    别人的图冲了）；`overwrite=True` 则复用那张画布并整图覆盖。

    `guard(fields)` 在**建任何东西之前**、拿到解析好的目标维度时调用，用来做数据范围
    校验。放在这里而不是让视图先算一遍：目标维度是「文件里的 code + 表单覆盖」两者
    叠出来的，视图自己再算一次必然与这里分叉，而分叉的表现是「校验放过了、落库落到
    了另一组维度」。事后校验更不行——事务已提交，图已经建出来了。
    """
    if not isinstance(payload, dict):
        raise GraphError('导入内容必须是一个 JSON 对象')
    version = payload.get('version')
    if version not in (None, EXPORT_VERSION):
        raise GraphError(f'不认识的导出格式版本：{version}（本实例支持 {EXPORT_VERSION}）')
    nodes_in = payload.get('nodes')
    if not isinstance(nodes_in, list) or not nodes_in:
        raise GraphError('导入内容里没有任何节点')
    edges_in = payload.get('edges') or []
    if not isinstance(edges_in, list):
        raise GraphError('edges 必须是数组')

    fields = _resolve_target_canvas_fields(payload, overrides)
    if guard is not None:
        guard(fields)
    existing = TopoCanvas.objects.filter(
        project_group=fields['project_group'], channel=fields['channel'],
        environment=fields['environment'], region=fields['region'], slug=fields['slug'],
    ).first()
    if existing is not None and not overwrite:
        raise GraphError(
            f'「{existing.dim_label}」下已存在标识为 {fields["slug"]} 的画布'
            f'（{existing.name}）。换一个标识，或勾选「覆盖已存在的画布」')
    if existing is not None:
        canvas = existing
        canvas.name = fields['name']
        canvas.description = fields['description']
        canvas.save(update_fields=['name', 'description', 'updated_at'])
    else:
        canvas = TopoCanvas.objects.create(created_by=user, **fields)

    report = {'skipped_nodes': [], 'skipped_edges': 0, 'overwritten': existing is not None}
    kept_keys = set()
    save_nodes = []
    for index, item in enumerate(nodes_in):
        if not isinstance(item, dict):
            raise GraphError(f'第 {index + 1} 个节点不是对象')
        key = str(item.get('key') or f'imported-{index}')
        source = item.get('source') or 'external'
        kind = item.get('kind') or ''
        label = (item.get('label') or '').strip()
        node = {
            'key': key, 'source': source, 'kind': kind, 'label': label,
            'sub_label': item.get('sub_label') or '', 'attrs': item.get('attrs') or {},
            'x': item.get('x') or 0, 'y': item.get('y') or 0,
            'pinned': bool(item.get('pinned')), 'remark': item.get('remark') or '',
        }
        if source == 'ref':
            resolved = _resolve_ref_for_import(kind, item.get('ref'), canvas, label, report)
            if resolved is None:
                continue
            node['ref_id'] = resolved
        kept_keys.add(key)
        save_nodes.append(node)

    if not save_nodes:
        raise GraphError('导入内容里的节点全部无法落地（引用的资源都不存在或与本画布维度不符）')

    save_edges = []
    for item in edges_in:
        if not isinstance(item, dict):
            continue
        src, dst = str(item.get('source') or ''), str(item.get('target') or '')
        # 端点被跳过的连线一并跳过：留着它就是一条指向空气的线
        if src not in kept_keys or dst not in kept_keys or src == dst:
            report['skipped_edges'] += 1
            continue
        save_edges.append({'source': src, 'target': dst,
                           'relation': item.get('relation') or 'custom',
                           'label': item.get('label') or '',
                           'attrs': item.get('attrs') or {},
                           # 连接点跟着走：不带的话克隆出来的图走线全弹回左右两侧
                           'source_handle': item.get('source_handle') or '',
                           'target_handle': item.get('target_handle') or ''})

    stats = graph.save_graph(
        canvas,
        {'nodes': save_nodes, 'edges': save_edges,
         'layout_mode': (payload.get('canvas') or {}).get('layout_mode')},
        user=user,
    )
    report.update(nodes=stats['nodes'], edges=stats['edges'])
    return canvas, report


def _resolve_ref_for_import(kind, key, canvas, label, report):
    """引用型节点的自然键 → 本库实体 id；落不了地则记一条跳过原因并返回 None。"""
    def skip(reason):
        report['skipped_nodes'].append({'kind': kind, 'label': label, 'reason': reason})
        return None

    if kind not in NATURAL_KEYS:
        return skip('不是可引用的平台资源类型')
    if not isinstance(key, dict) or not key:
        return skip('导出时该资源已失联，无法在本实例找回')
    _, resolve = NATURAL_KEYS[kind]
    obj = resolve(key)
    if obj is None:
        return skip('本实例找不到同名资源')
    if not dims_ok(kind, obj, canvas):
        return skip(f'与画布维度（{canvas.dim_label}）不符')
    return obj.id


# ──────────────────────────────────────────────────────────
# 克隆
# ──────────────────────────────────────────────────────────

def clone_canvas(canvas, overrides, user=None, guard=None):
    """按新维度克隆一张画布。

    走「导出 → 导入」同一条路，所以维度不符的主机会被跳过并报告，得到的是一份
    可复用的骨架（外部节点与连线）加上目标维度里确实存在的平台资源。
    """
    overrides = dict(overrides or {})
    if not overrides.get('name') and not overrides.get('slug'):
        raise GraphError('克隆必须指定新的画布名称或标识，否则会与原画布冲突')
    payload = export_graph(canvas)
    # 克隆是「另存一张」，不允许悄悄覆盖已有画布
    return import_graph(payload, overrides, user=user, overwrite=False, guard=guard)
