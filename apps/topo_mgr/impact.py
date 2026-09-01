"""影响面反查：给定一个平台实体，答「它在哪些画布上、上下游是谁」。

这是画布真正的运维价值所在。画得再漂亮的图，如果只能从画布这一侧打开，那它就是
一张挂在墙上的画；反查让它能从**故障那一侧**被用起来——手上有一台要下线的主机、
一个解析异常的域名，直接问平台「动它会波及什么」。

口径与整图取数共用 `graph.resolve_refs` / `graph.node_payload`：邻居节点的名称与
状态必须和画布上看到的一模一样，两套取数迟早分叉，而分叉的表现是「反查说它连着
A，打开画布却是 B」。

可见性同样按数据范围收窄（调用方传入已收窄的画布查询集）：反查不该成为绕过项目组
范围看别人拓扑的后门。
"""

from .models import TopoNode
from . import graph


def _brief(node, resolved):
    """邻居节点的精简信息：够认出是谁，不带坐标与自定义属性。"""
    full = graph.node_payload(node, resolved)
    return {
        'id': full['id'],
        'kind': full['kind'],
        'kind_label': full['kind_label'],
        'source': full['source'],
        'ref_id': full['ref_id'],
        'label': full['label'],
        'sub_label': full['sub_label'],
        'health': (full['ref'] or {}).get('health', 'none'),
        'status_label': (full['ref'] or {}).get('status_label', ''),
        'lost': bool(full['ref']) and full['ref'].get('exists') is False,
    }


def entity_label(kind, ref_id):
    """被查实体自己的名字（实体已删则返回 None，接口据此提示「已不存在」）。"""
    source = graph.REF_SOURCES.get(kind)
    if source is None:
        return None
    queryset, extract = source
    obj = queryset([ref_id]).first()
    return extract(obj)['label'] if obj is not None else None


def impact(kind, ref_id, canvases):
    """反查该实体出现在哪些画布上，以及每处的上下游。

    `canvases` 传已按调用方权限收窄的画布查询集（或可迭代对象）。
    """
    nodes = list(
        TopoNode.objects
        .filter(kind=kind, ref_id=ref_id, canvas__in=canvases)
        .select_related('canvas__project_group', 'canvas__channel', 'canvas__environment')
        .prefetch_related('in_edges__source', 'out_edges__target')
        .order_by('canvas_id')
    )
    if not nodes:
        return []

    # 邻居节点一次性批量解析：逐节点解析会让一台挂在五张图上的主机查上几十次库
    neighbours = []
    for node in nodes:
        neighbours.extend(edge.source for edge in node.in_edges.all())
        neighbours.extend(edge.target for edge in node.out_edges.all())
    resolved = graph.resolve_refs(neighbours)

    results = []
    for node in nodes:
        canvas = node.canvas
        results.append({
            'canvas': {
                'id': canvas.id,
                'name': canvas.name,
                'slug': canvas.slug,
                'dim_label': canvas.dim_label,
                'project_group_name': canvas.project_group.name,
                'channel_name': canvas.channel.name,
                'environment_name': canvas.environment.name,
                'is_prod': canvas.environment.is_prod,
                'region': canvas.region,
                # 深链口径与概览页一致（path + query），前端直接 router.push
                'link': {'path': f'/topo/canvases/{canvas.id}', 'query': {}},
            },
            'node': {'id': node.id, 'kind': node.kind, 'kind_label': node.kind_label},
            # upstream = 指向它的（谁的流量落到它身上），downstream = 它指向的
            'upstream': [
                {'relation': edge.relation,
                 'relation_label': edge.get_relation_display(),
                 'node': _brief(edge.source, resolved)}
                for edge in node.in_edges.all()
            ],
            'downstream': [
                {'relation': edge.relation,
                 'relation_label': edge.get_relation_display(),
                 'node': _brief(edge.target, resolved)}
                for edge in node.out_edges.all()
            ],
        })
    return results
