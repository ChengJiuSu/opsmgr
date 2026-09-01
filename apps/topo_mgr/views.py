"""拓扑画布接口。

权限走 `module_perm('topo')`（四档全开）：画布是运维自己维护的地图，「可看、可建
自己的图、但不能改别人的图」是真实诉求，`read_create` 这一档在这里有意义。

刻意**不叠 sudo**：画布不改变任何人的权限，也不下发任何命令；它引用的实体（主机、
域名、证书）在各自页面上该有的门槛一个不少。给一张示意图加二次验证只会让人不用它。

节点/连线不做独立的增删改接口，全部经 `/graph/` 整图读写——理由见 graph.py。
"""

from django.db.models import Q
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.views import APIView

from apps.auth_mgr import roles
from apps.auth_mgr.permissions import module_perm
from common.pagination import StandardPagination
from common.response import fail, ok

# 别名导入：`graph` 在 ViewSet 里是 @action 方法名，同名会读着别扭
from . import graph as graph_svc
from . import health as health_svc
from . import impact as impact_svc
from . import portable
from .models import (
    EXTERNAL_KIND_GROUPS,
    REF_KINDS,
    RELATIONS,
    TopoCanvas,
)
from .serializers import TopoCanvasSerializer


class TopoCanvasViewSet(viewsets.ModelViewSet):
    """画布 CRUD + 整图读写 + 自动编排 + 候选资源。"""

    queryset = (
        TopoCanvas.objects
        .select_related('project_group', 'channel', 'environment', 'created_by')
        .order_by('project_group__code', 'channel__code', 'environment__code', 'slug')
    )
    serializer_class = TopoCanvasSerializer
    permission_classes = [module_perm('topo')]
    pagination_class = StandardPagination

    # 查询参数 → 维度外键（纯数字按 id，否则按 code，与主机列表同口径）
    DICT_FILTERS = (
        ('project_group', 'project_group'),
        ('channel', 'channel'),
        ('env', 'environment'),
    )

    def get_queryset(self):
        qs = super().get_queryset()
        # 数据范围与主机列表同一套口径：范围为空 = 不限
        if hasattr(self.request.user, 'narrow'):
            qs = self.request.user.narrow(qs)
        params = self.request.query_params
        for param, field in self.DICT_FILTERS:
            value = params.get(param)
            if value:
                qs = qs.filter(**{f'{field}_id' if value.isdigit() else f'{field}__code': value})
        region = params.get('region')
        if region:
            qs = qs.filter(region=region)
        keyword = params.get('keyword')
        if keyword:
            qs = qs.filter(Q(name__icontains=keyword) | Q(slug__icontains=keyword)
                           | Q(description__icontains=keyword))
        return qs

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    def _check_scope(self, canvas):
        """单点维度校验：列表被收窄了，详情页也不能靠 URL 猜 id 绕过去。"""
        user = self.request.user
        if hasattr(user, 'allows_dims') and not user.allows_dims(
                canvas.project_group_id, canvas.environment_id):
            return fail('当前账号的数据范围不包含该画布所属的项目组/环境', status=403)
        return None

    @extend_schema(
        responses=OpenApiTypes.OBJECT,
        description='整图读取：节点（引用型节点带实时状态）+ 连线。')
    @extend_schema(
        methods=['PUT'], request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT,
        description='整图覆盖保存：nodes 每项须带前端生成的 key，edges 的 source/target 引用该 key。')
    @action(detail=True, methods=['get', 'put'])
    def graph(self, request, pk=None):
        canvas = self.get_object()
        denied = self._check_scope(canvas)
        if denied is not None:
            return denied
        if request.method == 'GET':
            data = {'canvas': self.get_serializer(canvas).data}
            data.update(graph_svc.build_graph(canvas))
            return ok(data)
        try:
            stats = graph_svc.save_graph(canvas, request.data, user=request.user)
        except graph_svc.GraphError as exc:
            return fail(str(exc))
        canvas.refresh_from_db()
        data = {'canvas': self.get_serializer(canvas).data, 'stats': stats}
        data.update(graph_svc.build_graph(canvas))
        return ok(data, msg=f'已保存 {stats["nodes"]} 个节点、{stats["edges"]} 条连线')

    @extend_schema(
        request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT,
        description='按画布维度自动拉取平台已有资源并生成节点与关系，幂等（已在图上的跳过）。')
    @action(detail=True, methods=['post'])
    def autofill(self, request, pk=None):
        canvas = self.get_object()
        denied = self._check_scope(canvas)
        if denied is not None:
            return denied
        stats = graph_svc.autofill(canvas)
        if not stats['servers']:
            return ok(stats, msg='该项目组/渠道/环境下还没有服务器，没有可自动编排的资源')
        data = {'stats': stats}
        data.update(graph_svc.build_graph(canvas))
        return ok(data, msg=f'新增 {stats["added_nodes"]} 个节点、{stats["added_edges"]} 条连线')

    @extend_schema(
        parameters=[
            OpenApiParameter('kind', str, description='server / domain / dns_record / certificate'),
            OpenApiParameter('keyword', str, description='名称模糊匹配'),
        ],
        responses=OpenApiTypes.OBJECT,
        description='可引用的平台资源候选（主机按画布维度收窄）。')
    @action(detail=True, methods=['get'])
    def candidates(self, request, pk=None):
        canvas = self.get_object()
        denied = self._check_scope(canvas)
        if denied is not None:
            return denied
        kind = request.query_params.get('kind') or 'server'
        try:
            items = graph_svc.candidates(
                canvas, kind, (request.query_params.get('keyword') or '').strip())
        except graph_svc.GraphError as exc:
            return fail(str(exc))
        # 已在画布上的资源标出来，前端置灰而不是让人选完才报「重复引用」
        used = set(canvas.nodes.filter(kind=kind, ref_id__isnull=False)
                   .values_list('ref_id', flat=True))
        for item in items:
            item['on_canvas'] = item['ref_id'] in used
        return ok({'kind': kind, 'results': items})

    @extend_schema(
        request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT,
        description='按新维度克隆本画布；维度不符的主机会被跳过并在报告里列出。')
    @action(detail=True, methods=['post'])
    def clone(self, request, pk=None):
        canvas = self.get_object()
        denied = self._check_scope(canvas)
        if denied is not None:
            return denied
        overrides = request.data if isinstance(request.data, dict) else {}
        try:
            created, report = portable.clone_canvas(
                canvas, overrides, user=request.user, guard=dims_guard(request.user))
        except portable.ScopeDenied as exc:
            return fail(str(exc), status=403)
        except portable.GraphError as exc:
            return fail(str(exc))
        return ok({'canvas': self.get_serializer(created).data, 'report': report},
                  msg=_report_msg('已克隆', report))

    @extend_schema(
        responses=OpenApiTypes.OBJECT,
        description='整图导出为可跨实例搬运的 JSON（引用型节点用自然键而非主键）。')
    @action(detail=True, methods=['get'])
    def export(self, request, pk=None):
        canvas = self.get_object()
        denied = self._check_scope(canvas)
        if denied is not None:
            return denied
        return ok(portable.export_graph(canvas))



def dims_guard(user):
    """克隆/导入的**目标**维度也要在数据范围内。

    只校验来源画布不够：范围内的图可以被克隆成一张范围外的图，等于借克隆把数据写进
    自己看不见的地方。
    """
    def guard(fields):
        if hasattr(user, 'allows_dims') and not user.allows_dims(
                fields['project_group'].id, fields['environment'].id):
            raise portable.ScopeDenied('当前账号的数据范围不包含目标项目组/环境')
    return guard


def _report_msg(prefix, report):
    """把导入/克隆的报告压成一句人话：跳过了什么必须说，否则没人会去翻 report。"""
    parts = [f'{report["nodes"]} 个节点、{report["edges"]} 条连线']
    skipped = report.get('skipped_nodes') or []
    if skipped:
        parts.append(f'跳过 {len(skipped)} 个节点（引用的资源不存在或维度不符）')
    if report.get('skipped_edges'):
        parts.append(f'跳过 {report["skipped_edges"]} 条连线')
    return f'{prefix} ' + '，'.join(parts)


class TopoImportView(APIView):
    """导入画布 JSON。

    覆盖已存在的画布额外要 `topo:write`：新建一张图是 read_create 档的正常动作，
    但把别人那张图整份冲掉不是——按 HTTP 方法推档会把两者都判成 POST/read_create。
    """

    permission_classes = [module_perm('topo')]

    @extend_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT)
    def post(self, request):
        body = request.data if isinstance(request.data, dict) else {}
        payload = body.get('payload')
        if payload is None:
            return fail('缺少 payload（导出得到的 JSON 内容）')
        overwrite = bool(body.get('overwrite'))
        if overwrite and not roles.user_can(request.user, 'topo', 'PUT'):
            return fail('覆盖已存在的画布需要拓扑画布的读写权限', status=403)
        overrides = body.get('overrides') or {}
        if not isinstance(overrides, dict):
            return fail('overrides 必须是对象')
        try:
            canvas, report = portable.import_graph(
                payload, overrides, user=request.user, overwrite=overwrite,
                guard=dims_guard(request.user))
        except portable.ScopeDenied as exc:
            return fail(str(exc), status=403)
        except portable.GraphError as exc:
            return fail(str(exc))
        return ok({'canvas': TopoCanvasSerializer(canvas).data, 'report': report},
                  msg=_report_msg('已导入', report))


class TopoHealthView(APIView):
    """画布健康度汇总：列表页的「健康」列与概览页的「拓扑异常」块共用。

    单独一个接口而不是塞进画布列表：列表是分页的、常被高频筛选，健康度要遍历所有
    引用型节点，两者绑在一起会让翻页也付一次汇总的代价。前端拉完列表再补这一发，
    汇总慢了最多是那一列晚出来，不拖住表格。
    """

    permission_classes = [module_perm('topo')]

    @extend_schema(
        parameters=[
            OpenApiParameter('only_issues', bool,
                             description='只回传有异常/警告的画布'),
        ],
        responses=OpenApiTypes.OBJECT)
    def get(self, request):
        canvases = TopoCanvas.objects.select_related(
            'project_group', 'channel', 'environment')
        if hasattr(request.user, 'narrow'):
            canvases = request.user.narrow(canvases)
        results = health_svc.summarize(canvases)
        if request.query_params.get('only_issues') == 'true':
            results = [r for r in results if r['danger'] or r['warning']]
        return ok({
            'total': len(results),
            'danger': sum(1 for r in results if r['danger']),
            'warning': sum(1 for r in results if r['warning'] and not r['danger']),
            'results': results,
        })


class TopoImpactView(APIView):
    """影响面反查：这台主机 / 这个域名在哪些画布上，上下游是谁。

    入口挂在服务器页与域名页，而不是只能从画布那侧打开——手上有一台要下线的主机时，
    人不会先想到「去翻一遍拓扑图」。
    """

    permission_classes = [module_perm('topo')]

    @extend_schema(
        parameters=[
            OpenApiParameter('kind', str, required=True,
                             description='server / domain / dns_record / certificate'),
            OpenApiParameter('ref_id', int, required=True, description='平台实体主键'),
        ],
        responses=OpenApiTypes.OBJECT)
    def get(self, request):
        kind = request.query_params.get('kind') or ''
        ref_id = request.query_params.get('ref_id') or ''
        if kind not in REF_KINDS:
            return fail(f'「{kind}」不是可引用的平台资源类型；'
                        f'可选：{"、".join(REF_KINDS)}')
        if not str(ref_id).isdigit():
            return fail('ref_id 必须是平台实体的主键')
        canvases = TopoCanvas.objects.all()
        # 反查不该成为绕过数据范围看别人拓扑的后门
        if hasattr(request.user, 'narrow'):
            canvases = request.user.narrow(canvases)
        results = impact_svc.impact(kind, int(ref_id), canvases)
        return ok({
            'kind': kind,
            'kind_label': REF_KINDS[kind],
            'ref_id': int(ref_id),
            'label': impact_svc.entity_label(kind, int(ref_id)),
            'total': len(results),
            'results': results,
        })


class TopoMetaView(APIView):
    """节点类型与关系登记表：左侧调色板与关系下拉由后端驱动。

    前端不再写死一份类型清单——加一种负载均衡只改 models.EXTERNAL_KIND_GROUPS，
    否则每次都要前后端各改一处，漏改的表现是「拖得出来但存不进去」。
    """

    permission_classes = [module_perm('topo')]

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request):
        return ok({
            'ref_kinds': [{'kind': k, 'label': v} for k, v in REF_KINDS.items()],
            'external_groups': [
                {'group': group,
                 'kinds': [{'kind': k, 'label': label} for k, label in members]}
                for group, members in EXTERNAL_KIND_GROUPS
            ],
            'relations': [{'value': v, 'label': label} for v, label in RELATIONS],
        })
