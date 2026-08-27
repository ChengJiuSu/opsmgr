"""配置管理：服务器的项目组 / 环境 / 渠道 / 云厂商批量归属分配 + 服务类型规划。

本模块不建模型，数据以 asset_mgr 为唯一数据源（与 dashboard 同为纯视图 app）。
"""
from django.db import transaction
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework.views import APIView

from apps.asset_mgr.models import (
    Channel,
    CloudProvider,
    Environment,
    ProjectGroup,
    Server,
    ServerService,
    ServiceType,
)
from apps.auth_mgr.permissions import module_perm
from common.response import fail, ok

# 可批量分配的字典外键：请求键 → (模型, 展示名)
ASSIGN_DICT_FIELDS = {
    'project_group': (ProjectGroup, '项目组'),
    'environment': (Environment, '环境'),
    'channel': (Channel, '渠道'),
    'cloud_provider': (CloudProvider, '云厂商'),
}
# 地域为普通文本字段，单独处理
ASSIGN_TEXT_FIELDS = ('region',)


class BatchAssignView(APIView):
    """批量分配：把选中的服务器归属到项目组/环境/渠道/云厂商。

    请求体::

        {
          "server_ids": [1, 2, 3],
          "assign": {
            "project_group": 2,        # 提供且非 null → 设置
            "environment": null,       # 提供且为 null → 清空
            "region": "cn-hangzhou"    # 文本字段，"" 表示清空
          }                            # 未提供的字段保持不动
        }
    """

    permission_classes = [module_perm('cmdb', min_level='write')]

    @extend_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT)
    def post(self, request):
        server_ids = request.data.get('server_ids')
        assign = request.data.get('assign')

        # ---------- 基础校验 ----------
        if not isinstance(server_ids, list) or not server_ids:
            return fail('server_ids 必须为非空数组')
        if not isinstance(assign, dict) or not assign:
            return fail('assign 必须为非空对象')
        unknown = set(assign) - set(ASSIGN_DICT_FIELDS) - set(ASSIGN_TEXT_FIELDS)
        if unknown:
            return fail(f'不支持的分配字段：{", ".join(sorted(unknown))}')

        servers = Server.objects.filter(id__in=server_ids)
        if servers.count() != len(set(server_ids)):
            return fail('存在无效的服务器 id')

        # ---------- 组装更新字段 ----------
        update_fields = {}
        for key, (model, label) in ASSIGN_DICT_FIELDS.items():
            if key not in assign:
                continue
            value = assign[key]
            if value is None:
                update_fields[f'{key}_id'] = None
                continue
            # 字典项必须存在且处于启用状态
            if not model.objects.filter(id=value, enabled=True).exists():
                return fail(f'{label}不存在或已停用')
            update_fields[f'{key}_id'] = value
        for key in ASSIGN_TEXT_FIELDS:
            if key in assign:
                update_fields[key] = str(assign[key] or '')

        # ---------- 批量更新（.update 不触发 auto_now，手动补时间戳） ----------
        with transaction.atomic():
            updated = servers.update(**update_fields, updated_at=timezone.now())
        return ok({'updated': updated})


def _validate_batch_params(server_ids, service_type_id):
    """服务规划/流转/取消接口的公共入参校验，返回 (servers, service_type, error)。"""
    if not isinstance(server_ids, list) or not server_ids:
        return None, None, fail('server_ids 必须为非空数组')
    servers = Server.objects.filter(id__in=server_ids)
    if servers.count() != len(set(server_ids)):
        return None, None, fail('存在无效的服务器 id')
    service_type = ServiceType.objects.filter(id=service_type_id).first()
    if service_type is None:
        return None, None, fail('服务类型不存在')
    return servers, service_type, None


class ServicePlanView(APIView):
    """批量规划服务：为选中主机创建服务类型的 planned 记录（部署前置步骤）。

    请求体::

        {"server_ids": [1, 2], "service_type_ids": [3, 4], "process_count": 2}

    - process_count 可选（默认 1），应用到本次创建的所有记录
    - 进程名自动生成：<服务类型code>-<两位序号>，序号在相同云厂商+项目组+
      环境+渠道的主机范围内按服务类型递增（见 ServerService.next_process_name）
    - 幂等：主机已存在该服务类型记录（任意状态）时跳过，不报错
    """

    permission_classes = [module_perm('cmdb', min_level='write')]

    @extend_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT)
    def post(self, request):
        server_ids = request.data.get('server_ids')
        service_type_ids = request.data.get('service_type_ids')
        process_count = request.data.get('process_count', 1)
        if not isinstance(server_ids, list) or not server_ids:
            return fail('server_ids 必须为非空数组')
        if not isinstance(service_type_ids, list) or not service_type_ids:
            return fail('service_type_ids 必须为非空数组')
        if not isinstance(process_count, int) or isinstance(process_count, bool) or process_count < 1:
            return fail('process_count 必须为正整数')

        servers = Server.objects.filter(id__in=server_ids)
        if servers.count() != len(set(server_ids)):
            return fail('存在无效的服务器 id')
        # 规划仅允许选择启用中的服务类型
        types = ServiceType.objects.filter(id__in=service_type_ids, enabled=True)
        if types.count() != len(set(service_type_ids)):
            return fail('存在无效或已停用的服务类型')

        # 已存在的 (server, service_type) 组合跳过，保证幂等
        existing = set(
            ServerService.objects
            .filter(server__in=servers, service_type__in=types)
            .values_list('server_id', 'service_type_id')
        )
        pairs = [
            (srv, st)
            for srv in servers for st in types
            if (srv.id, st.id) not in existing
        ]
        with transaction.atomic():
            links = ServerService.build_planned_links(pairs, process_count=process_count)
            ServerService.objects.bulk_create(links)
        return ok({
            'created': len(links),
            'skipped': len(existing),
            'process_names': {f'{link.server_id}-{link.service_type_id}': link.process_name for link in links},
        })


class ServiceMarkView(APIView):
    """服务状态流转：planned→deployed→offline→planned（deployed→planned 允许回退纠错）。

    请求体::

        {"server_ids": [1, 2], "service_type_id": 3, "status": "deployed"}
    """

    permission_classes = [module_perm('cmdb', min_level='write')]

    @extend_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT)
    def post(self, request):
        target = request.data.get('status')
        valid_status = {key for key, _ in ServerService.STATUS_CHOICES}
        if target not in valid_status:
            return fail(f'status 必须为 {" / ".join(sorted(valid_status))}')
        servers, service_type, error = _validate_batch_params(
            request.data.get('server_ids'), request.data.get('service_type_id'),
        )
        if error:
            return error

        links = ServerService.objects.filter(server__in=servers, service_type=service_type)
        if links.count() != servers.count():
            return fail(f'部分主机尚未规划服务「{service_type.name}」，请先完成规划')
        # 校验所有记录都允许流转到目标状态（同状态视为无操作，直接放行）
        blocked = [
            link for link in links
            if link.status != target and target not in ServerService.STATUS_TRANSITIONS[link.status]
        ]
        if blocked:
            status_map = dict(ServerService.STATUS_CHOICES)
            froms = '、'.join(sorted({status_map[link.status] for link in blocked}))
            return fail(f'{len(blocked)} 条记录处于「{froms}」状态，不允许流转为「{status_map[target]}」')

        # 时间戳记录「最近一次进入该状态」的时间；.update 不触发 auto_now，手动补
        stamp_field = {'planned': 'planned_at', 'deployed': 'deployed_at', 'offline': 'offline_at'}[target]
        with transaction.atomic():
            updated = links.exclude(status=target).update(
                status=target, **{stamp_field: timezone.now()}, updated_at=timezone.now(),
            )
        return ok({'updated': updated})


class ServiceUnplanView(APIView):
    """取消规划：删除 planned 状态的记录；已部署/已下线记录保留追溯，禁止删除。

    请求体::

        {"server_ids": [1, 2], "service_type_id": 3}
    """

    permission_classes = [module_perm('cmdb', min_level='write')]

    @extend_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT)
    def post(self, request):
        servers, service_type, error = _validate_batch_params(
            request.data.get('server_ids'), request.data.get('service_type_id'),
        )
        if error:
            return error
        links = ServerService.objects.filter(server__in=servers, service_type=service_type)
        protected = links.exclude(status='planned').count()
        if protected:
            return fail(f'{protected} 条记录已部署或已下线，仅「已规划」状态可取消规划')
        with transaction.atomic():
            deleted, _ = links.delete()
        return ok({'deleted': deleted})


class ServiceProcessView(APIView):
    """修改进程数：更新已有服务记录的 process_count（规划创建后调整进程数量用）。

    请求体::

        {"server_ids": [1, 2], "service_type_id": 3, "process_count": 4}

    - **进程数只允许调大**：目标值小于任一记录当前进程数时整批拒绝（缩容需走下线流程）
    - 进程名不在此修改：进程名由规划时自动生成，保持稳定不随进程数变化
    """

    permission_classes = [module_perm('cmdb', min_level='write')]

    @extend_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT)
    def post(self, request):
        process_count = request.data.get('process_count')
        if not isinstance(process_count, int) or isinstance(process_count, bool) or process_count < 1:
            return fail('process_count 必须为正整数')
        servers, service_type, error = _validate_batch_params(
            request.data.get('server_ids'), request.data.get('service_type_id'),
        )
        if error:
            return error

        links = ServerService.objects.filter(server__in=servers, service_type=service_type)
        if links.count() != servers.count():
            return fail(f'部分主机尚未规划服务「{service_type.name}」，请先完成规划')
        blocked = links.filter(process_count__gt=process_count).count()
        if blocked:
            return fail(f'{blocked} 条记录当前进程数大于 {process_count}，进程数仅允许调大，不允许调小')
        with transaction.atomic():
            updated = links.filter(process_count__lt=process_count).update(
                process_count=process_count, updated_at=timezone.now(),
            )
        return ok({'updated': updated})


class AllocationOverviewView(APIView):
    """分配概览：各维度未分配数量与分布，供配置管理页顶部卡片展示。"""

    permission_classes = [module_perm('cmdb')]

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request):
        qs = Server.objects.all()
        unassigned = {
            key: qs.filter(**{f'{key}__isnull': True}).count()
            for key in ASSIGN_DICT_FIELDS
        }
        # 未规划任何服务的主机数（与归属维度统一放进 unassigned，前端卡片直接取用）
        unassigned['service_type'] = qs.filter(services__isnull=True).count()

        # 服务类型分布：按 服务类型×状态 计数
        by_service = {}
        service_rows = ServerService.objects.values_list('service_type__name', 'status')
        for name, status in service_rows:
            by_service.setdefault(name, {'planned': 0, 'deployed': 0, 'offline': 0})
            by_service[name][status] += 1

        by_project, by_env, by_channel = {}, {}, {}
        rows = qs.values('project_group__name', 'environment__name', 'channel__name')
        for row in rows:
            for target, field in (
                (by_project, 'project_group__name'),
                (by_env, 'environment__name'),
                (by_channel, 'channel__name'),
            ):
                name = row[field] or '未分配'
                target[name] = target.get(name, 0) + 1

        return ok({
            'total': qs.count(),
            'unassigned': unassigned,
            'by_project': by_project,
            'by_env': by_env,
            'by_channel': by_channel,
            'by_service': by_service,
        })
