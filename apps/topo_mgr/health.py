"""画布健康度汇总：把「图上有几个节点在冒烟」变成一个能放在列表和首页上的数字。

## 为什么不另写一套判定

汇总直接复用 `graph.resolve_refs`（按 kind 批查，4 条 SQL 封顶，与画布张数无关）与各
kind 的 `issues` 清单。换成「为汇总单独写几条 filter」看着更省，但那就有了第二套判定
口径——它一定会和画布上的颜色对不上，而对不上的表现是「首页说 3 个异常，点进去图上
只有 2 个红的」，之后没人再信这个数字。

## 只看引用型节点

外部节点（GA、负载均衡）是手工录入的，平台不知道它们的真实状态，凭空给个「健康」比
不给更糟——看图的人会以为那一层被监控着。它们在汇总里既不算健康也不算异常。
"""

from . import graph
from .models import TopoNode

#: 首页与列表页每张画布最多回传几条明细：给「点进去之前」的信息量就够
ISSUE_LIMIT = 5


def _canvas_brief(canvas):
    return {
        'id': canvas.id,
        'name': canvas.name,
        'slug': canvas.slug,
        'dim_label': canvas.dim_label,
        'is_prod': canvas.environment.is_prod,
        'link': {'path': f'/topo/canvases/{canvas.id}', 'query': {}},
    }


def summarize(canvases):
    """按画布汇总健康度，返回列表（有异常的排前面，同级按维度排）。

    `canvases` 传已按调用方权限收窄的查询集。
    """
    canvases = list(canvases)
    if not canvases:
        return []
    by_id = {c.id: c for c in canvases}
    nodes = list(
        TopoNode.objects
        .filter(canvas_id__in=by_id, source='ref')
        .only('id', 'canvas_id', 'kind', 'ref_id', 'label', 'sub_label',
              'source', 'attrs', 'x', 'y', 'pinned', 'remark')
    )
    resolved = graph.resolve_refs(nodes)

    buckets = {
        cid: {'canvas': _canvas_brief(canvas), 'total': 0, 'healthy': 0,
              'warning': 0, 'danger': 0, 'lost': 0, 'issues': []}
        for cid, canvas in by_id.items()
    }
    for node in nodes:
        bucket = buckets[node.canvas_id]
        bucket['total'] += 1
        info = resolved.get((node.kind, node.ref_id))
        if info is None:
            bucket['lost'] += 1
            bucket['danger'] += 1
            bucket['issues'].append({
                'node_id': node.id, 'kind': node.kind, 'kind_label': node.kind_label,
                'label': node.label, 'level': 'danger', 'text': '引用的资源已不存在',
            })
            continue
        health = info.get('health', 'healthy')
        bucket[health if health in ('warning', 'danger') else 'healthy'] += 1
        for issue in info.get('issues') or []:
            bucket['issues'].append({
                'node_id': node.id, 'kind': node.kind, 'kind_label': node.kind_label,
                'label': info['label'], 'level': issue['level'], 'text': issue['text'],
            })

    results = []
    for bucket in buckets.values():
        # 明细里 danger 优先：截断到 ISSUE_LIMIT 时先保住严重的那几条
        bucket['issues'].sort(key=lambda i: 0 if i['level'] == 'danger' else 1)
        bucket['issue_total'] = len(bucket['issues'])
        bucket['issues'] = bucket['issues'][:ISSUE_LIMIT]
        results.append(bucket)
    results.sort(key=lambda b: (-b['danger'], -b['warning'], b['canvas']['dim_label']))
    return results


def alert_items(canvases, limit=ISSUE_LIMIT):
    """概览页「拓扑异常」块的明细：每张有异常的画布一条，深链指进那张图。

    条目粒度是**画布**而不是节点：首页要回答的是「哪张链路图在冒烟」，逐节点列出来会
    把一台机的三个问题铺成三行，把别的异常挤出可见范围。
    """
    items = []
    for bucket in summarize(canvases):
        if not (bucket['danger'] or bucket['warning']):
            continue
        parts = []
        if bucket['danger']:
            parts.append(f'{bucket["danger"]} 个异常')
        if bucket['warning']:
            parts.append(f'{bucket["warning"]} 个警告')
        items.append({
            'name': f'{bucket["canvas"]["name"]}（{bucket["canvas"]["dim_label"]}）',
            'detail': '、'.join(parts) + '：' + '；'.join(
                i['text'] for i in bucket['issues'][:2]),
            'level': 'danger' if bucket['danger'] else 'warning',
            'link': bucket['canvas']['link'],
        })
    return items[:limit], len(items)
