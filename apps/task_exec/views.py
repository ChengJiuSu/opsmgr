import os

from django.conf import settings
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.views import APIView

from common.permissions import IsAdmin, IsAdminOrReadOnly
from common.response import fail, ok

from .models import Credential, ExecutionTask, ExecVariable
from .serializers import (
    CredentialSerializer,
    ExecutionTaskSerializer,
    ExecVariableSerializer,
    ExecVariableVersionSerializer,
    TaskExecutionLogDetailSerializer,
    TaskExecutionLogSerializer,
    validate_service_planned,
)
from .tasks import dispatch


# 剧本扫描时跳过的目录：Ansible 工程的非剧本组成部分
PLAYBOOK_EXCLUDE_DIRS = {
    'roles', 'inventories', 'inventory', 'group_vars', 'host_vars',
    'collections', 'files', 'templates', 'vars', 'library', 'filter_plugins',
}


class PlaybookListView(APIView):
    """列出 ANSIBLE_PROJECT_DIR 内可选的剧本文件（相对路径，供任务表单下拉）。"""

    permission_classes = [IsAdmin]

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request):
        project_dir = getattr(settings, 'ANSIBLE_PROJECT_DIR', '')
        if not project_dir or not os.path.isdir(project_dir):
            return fail(f'Ansible 项目目录不存在：{project_dir!r}（请配置 ANSIBLE_PROJECT_DIR）')
        playbooks = []
        for root, dirs, files in os.walk(project_dir):
            # 原地过滤，阻止 os.walk 深入非剧本目录与隐藏目录
            dirs[:] = [d for d in dirs if d not in PLAYBOOK_EXCLUDE_DIRS and not d.startswith('.')]
            for filename in sorted(files):
                if filename.endswith(('.yml', '.yaml')) and not filename.startswith('.'):
                    playbooks.append(os.path.relpath(os.path.join(root, filename), project_dir))
        return ok({'results': sorted(playbooks)})


class CredentialViewSet(viewsets.ModelViewSet):
    """连接凭据管理（仅管理员）。"""

    queryset = Credential.objects.all()
    serializer_class = CredentialSerializer
    permission_classes = [IsAdmin]


class ExecVariableViewSet(viewsets.ModelViewSet):
    """执行变量管理：按任务/模块有效维度加载注入 --extra-vars；亦供外部系统拉取。

    普通用户只读，管理员可维护；每次保存/回滚落版本快照（默认保留 10 版）。
    """

    queryset = ExecVariable.objects.select_related('project_group', 'channel', 'environment')
    serializer_class = ExecVariableSerializer
    permission_classes = [IsAdminOrReadOnly]

    # 查询参数 → 维度外键映射（纯数字按 id、否则按 code，与服务器接口一致）
    DICT_FILTERS = (
        ('project_group', 'project_group'),
        ('channel', 'channel'),
        ('env', 'environment'),
    )

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        enabled = params.get('enabled')
        keyword = params.get('keyword')
        if enabled in ('true', 'false'):
            qs = qs.filter(enabled=(enabled == 'true'))
        for param, field in self.DICT_FILTERS:
            value = params.get(param)
            if value:
                if value.isdigit():
                    qs = qs.filter(**{f'{field}_id': value})
                else:
                    qs = qs.filter(**{f'{field}__code': value})
        if keyword:
            from django.db.models import Q
            qs = qs.filter(Q(key__icontains=keyword) | Q(description__icontains=keyword))
        return qs

    @action(detail=True, methods=['get'])
    def versions(self, request, pk=None):
        """历史版本列表（新→旧，最多保留 EXEC_VARIABLE_VERSION_LIMIT 版）。"""
        variable = self.get_object()
        qs = variable.versions.select_related('created_by', 'project_group', 'channel', 'environment')
        return ok(ExecVariableVersionSerializer(qs, many=True).data)

    @action(detail=True, methods=['post'], permission_classes=[IsAdmin])
    def rollback(self, request, pk=None):
        """回滚到指定历史版本：套用快照内容并生成一条新版本记录。"""
        from .models import snapshot_variable

        variable = self.get_object()
        version_no = request.data.get('version')
        version = variable.versions.filter(version=version_no).first()
        if version is None:
            return fail(f'版本 {version_no} 不存在（超出保留范围或参数有误）')
        variable.value = version.value
        variable.description = version.description
        variable.enabled = version.enabled
        # 维度快照为弱关联：字典项已被删除的维度保持当前值不动
        for field in ('project_group_id', 'channel_id', 'environment_id'):
            snap = getattr(version, field)
            if snap is not None:
                setattr(variable, field, snap)
        variable.save()
        snapshot_variable(variable, user=request.user)
        return ok(ExecVariableSerializer(variable).data, msg=f'已回滚到版本 {version_no}')


class ExecutionTaskViewSet(viewsets.ModelViewSet):
    """任务模块：新建/编辑仅保存定义，执行走 execute 动作（主机按维度实时匹配）。

    普通用户只读，管理员可保存/编辑/执行。
    """

    queryset = (
        ExecutionTask.objects
        .select_related('credential', 'created_by', 'project_group', 'channel', 'environment')
        .prefetch_related(
            'servers',
            'steps__method', 'steps__project_group', 'steps__channel', 'steps__environment',
        )
    )
    serializer_class = ExecutionTaskSerializer
    permission_classes = [IsAdminOrReadOnly]
    http_method_names = ['get', 'post', 'put', 'patch', 'delete', 'head', 'options']

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        task_type = params.get('task_type')
        status = params.get('status')
        keyword = params.get('keyword')
        if task_type:
            # 类型下沉到模块后，筛选语义 = 包含该类型模块的任务
            qs = qs.filter(steps__task_type=task_type).distinct()
        if status:
            qs = qs.filter(status=status)
        if keyword:
            qs = qs.filter(name__icontains=keyword)
        return qs

    def perform_create(self, serializer):
        # 仅保存任务定义，不触发执行
        serializer.save(created_by=self.request.user)

    def update(self, request, *args, **kwargs):
        if self.get_object().status == 'running':
            return fail('任务正在执行中，无法编辑')
        return super().update(request, *args, **kwargs)

    @action(detail=True, methods=['get'])
    def matched_servers(self, request, pk=None):
        """执行表单数据源：全局范围匹配主机 + 各模块的主机来源与自定义匹配结果。"""
        from apps.asset_mgr.serializers import ServerSerializer

        task = self.get_object()
        global_servers = list(task.matched_servers())
        steps = []
        for step in task.steps.all():
            entry = {
                'order': step.order, 'name': step.name, 'task_type': step.task_type,
                'task_type_display': step.get_task_type_display(),
                'custom': step.use_custom_scope,
            }
            if step.use_custom_scope:
                servers = list(step.matched_servers())
                entry['total'] = len(servers)
                entry['servers'] = ServerSerializer(servers, many=True).data
            else:
                entry['total'] = len(global_servers)
            steps.append(entry)
        return ok({
            'global': {'results': ServerSerializer(global_servers, many=True).data, 'total': len(global_servers)},
            'steps': steps,
        })

    @action(detail=True, methods=['post'])
    def execute(self, request, pk=None):
        """执行任务流。body.server_ids 可选，仅作用于全局范围模块（缺省=全部全局匹配主机）；
        自定义范围模块按其维度全量执行。逐模块校验主机非空与方法服务规划。"""
        task = self.get_object()
        if task.status == 'running':
            return fail('任务正在执行中，请等待完成后再发起')
        steps = list(task.steps.all())
        if not steps:
            return fail('任务未配置任何模块，请先编辑任务添加模块')
        # 兜底存量任务：主机范围三维度必选，未选齐不予执行
        if not (task.project_group_id and task.channel_id and task.environment_id):
            return fail('任务未配置完整的主机范围（项目组/渠道/环境必选），请先编辑任务补全')

        # 全局范围主机：维度匹配 ∩ 勾选子集
        matched = {s.id: s for s in task.matched_servers()}
        server_ids = request.data.get('server_ids')
        if server_ids is None:
            selected_global = list(matched.values())
        else:
            if not isinstance(server_ids, list):
                return fail('server_ids 必须为主机 id 数组（缺省表示全部全局匹配主机）')
            invalid = [i for i in server_ids if i not in matched]
            if invalid:
                return fail(f'主机 id {invalid} 不在全局范围匹配集内，请刷新执行页面后重试')
            selected_global = [matched[i] for i in server_ids]

        # 逐模块解析主机并校验执行条件
        for step in steps:
            servers = list(step.matched_servers()) if step.use_custom_scope else selected_global
            if not servers:
                scope = '自定义范围维度未匹配到可执行主机' if step.use_custom_scope \
                    else '全局范围未匹配/未勾选任何主机'
                return fail(f'模块「{step.name}」{scope}，无法执行')
            if step.task_type == 'method':
                error = validate_service_planned(step.method, servers)
                if error:
                    return fail(f'模块「{step.name}」：{error}')

        # 固化全局范围主机到任务快照，重置最近一次执行状态
        task.servers.set(selected_global)
        task.status = 'pending'
        task.return_code = None
        task.output = ''
        task.started_at = None
        task.finished_at = None
        task.save(update_fields=['status', 'return_code', 'output', 'started_at', 'finished_at'])
        log = dispatch(task, triggered_by=request.user, trigger_type='execute')
        return ok(
            {'task': ExecutionTaskSerializer(task).data, 'log_id': log.id, 'step_count': len(steps)},
            msg=f'已发起执行（{len(steps)} 个模块，{task.get_exec_mode_display()}）',
        )

    @action(detail=True, methods=['get'])
    def output(self, request, pk=None):
        """轻量轮询接口：仅返回状态与输出。"""
        task = self.get_object()
        return ok({
            'id': task.id,
            'status': task.status,
            'return_code': task.return_code,
            'output': task.output,
            'duration': task.duration,
        })

    @action(detail=True, methods=['get'])
    def logs(self, request, pk=None):
        """任务执行历史列表（每次执行一条日志，不含输出大字段）。"""
        task = self.get_object()
        qs = task.logs.select_related('triggered_by')
        return ok(TaskExecutionLogSerializer(qs, many=True).data)

    @action(detail=True, methods=['get'], url_path=r'logs/(?P<log_id>\d+)')
    def log_detail(self, request, pk=None, log_id=None):
        """单条执行日志详情，含完整输出（超长部分从落盘文件读取）。"""
        task = self.get_object()
        log = get_object_or_404(task.logs.select_related('triggered_by'), pk=log_id)
        return ok(TaskExecutionLogDetailSerializer(log).data)
