from django.db.models import ProtectedError, Q
from rest_framework import viewsets
from rest_framework.decorators import action

from common.permissions import IsAdminOrReadCreate
from common.response import fail, ok

from .models import Channel, CloudProvider, Environment, ProjectGroup, Server, ServiceType
from .serializers import (
    ChannelSerializer,
    CloudProviderSerializer,
    EnvironmentSerializer,
    ProjectGroupSerializer,
    ServerSerializer,
    ServiceTypeSerializer,
)


class BaseDictViewSet(viewsets.ModelViewSet):
    """CMDB 字典表通用 ViewSet：启用/关键字筛选 + 被引用删除保护。

    权限：已认证用户可查看/新增，修改与删除仅管理员。
    """

    permission_classes = [IsAdminOrReadCreate]
    # 删除保护提示中的引用对象单位（如「台服务器」「条主机服务记录」）
    protected_hint = '台服务器'

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        enabled = params.get('enabled')
        keyword = params.get('keyword')
        if enabled in ('true', 'false'):
            qs = qs.filter(enabled=enabled == 'true')
        if keyword:
            qs = qs.filter(Q(name__icontains=keyword) | Q(code__icontains=keyword))
        return qs

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        try:
            return super().destroy(request, *args, **kwargs)
        except ProtectedError as exc:
            # PROTECT 外键约束：字典项仍被引用时禁止删除，计数取自约束异常本身
            count = len(exc.protected_objects)
            return fail(f'「{instance.name}」仍被 {count} {self.protected_hint}引用，无法删除')


class ProjectGroupViewSet(BaseDictViewSet):
    queryset = ProjectGroup.objects.all()
    serializer_class = ProjectGroupSerializer


class ChannelViewSet(BaseDictViewSet):
    queryset = Channel.objects.all()
    serializer_class = ChannelSerializer


class EnvironmentViewSet(BaseDictViewSet):
    queryset = Environment.objects.all()
    serializer_class = EnvironmentSerializer


class CloudProviderViewSet(BaseDictViewSet):
    queryset = CloudProvider.objects.all()
    serializer_class = CloudProviderSerializer


class ServiceTypeViewSet(BaseDictViewSet):
    queryset = ServiceType.objects.all()
    serializer_class = ServiceTypeSerializer
    # 服务类型被 ServerService（及 method_mgr.Method）以 PROTECT 引用
    protected_hint = '条记录'


class ServerViewSet(viewsets.ModelViewSet):
    queryset = (
        Server.objects
        .select_related('project_group', 'channel', 'environment', 'cloud_provider')
        .prefetch_related('services__service_type')
        .order_by('-created_at')
    )
    serializer_class = ServerSerializer
    # 已认证用户可查看/新增服务器，修改与删除仅管理员
    permission_classes = [IsAdminOrReadCreate]

    # 查询参数 → 字典外键字段映射（值为纯数字按 id 过滤，否则按 code 过滤）
    DICT_FILTERS = (
        ('env', 'environment'),
        ('project_group', 'project_group'),
        ('channel', 'channel'),
        ('cloud_provider', 'cloud_provider'),
    )

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        status = params.get('status')
        keyword = params.get('keyword')
        if status:
            qs = qs.filter(status=status)
        for param, field in self.DICT_FILTERS:
            value = params.get(param)
            if value:
                if value.isdigit():
                    qs = qs.filter(**{f'{field}_id': value})
                else:
                    qs = qs.filter(**{f'{field}__code': value})
        # 服务类型/状态筛选：按主机服务关联记录过滤，多条命中时去重
        service_type = params.get('service_type')
        service_status = params.get('service_status')
        if service_type:
            cond = (
                {'services__service_type_id': service_type}
                if service_type.isdigit()
                else {'services__service_type__code': service_type}
            )
            if service_status:
                cond['services__status'] = service_status
            qs = qs.filter(**cond).distinct()
        elif service_status:
            qs = qs.filter(services__status=service_status).distinct()
        # 已规划筛选：assigned=service_type → 仅返回有服务记录的主机（服务器分配页专用）
        if params.get('assigned') == 'service_type':
            qs = qs.filter(services__isnull=False).distinct()
        # 可执行筛选：executable=true → 仅返回已规划/已部署服务的主机
        # （任务执行表单专用，与 task_exec 执行权限校验口径一致，offline 不算）
        if params.get('executable') == 'true':
            qs = qs.filter(services__status__in=('planned', 'deployed')).distinct()
        # 未分配筛选：unassigned=<维度字段名> / any（任一归属维度缺失）/ service_type（未规划任何服务）
        unassigned = params.get('unassigned')
        dict_fields = [field for _, field in self.DICT_FILTERS]
        if unassigned == 'any':
            cond = Q()
            for field in dict_fields:
                cond |= Q(**{f'{field}__isnull': True})
            qs = qs.filter(cond)
        elif unassigned in dict_fields:
            qs = qs.filter(**{f'{unassigned}__isnull': True})
        elif unassigned == 'service_type':
            qs = qs.filter(services__isnull=True)
        if keyword:
            qs = qs.filter(Q(name__icontains=keyword) | Q(ip__icontains=keyword))
        return qs

    @action(detail=False, methods=['get'])
    def stats(self, request):
        """服务器维度统计：按状态、环境、项目组分组计数。"""
        qs = self.get_queryset()
        by_status = {key: 0 for key, _ in Server.STATUS_CHOICES}
        by_env = {}
        by_project = {}
        for srv in qs.values('status', 'environment__name', 'project_group__name'):
            by_status[srv['status']] = by_status.get(srv['status'], 0) + 1
            env_name = srv['environment__name'] or '未分配'
            by_env[env_name] = by_env.get(env_name, 0) + 1
            project_name = srv['project_group__name'] or '未分配'
            by_project[project_name] = by_project.get(project_name, 0) + 1
        return ok({
            'total': qs.count(),
            'by_status': by_status,
            'by_env': by_env,
            'by_project': by_project,
        })
