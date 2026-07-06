from django.shortcuts import get_object_or_404
from rest_framework import viewsets
from rest_framework.decorators import action

from common.permissions import IsAdmin, IsAdminOrReadOnly
from common.response import fail, ok

from .models import Credential, ExecutionTask
from .serializers import (
    CredentialSerializer,
    ExecutionTaskSerializer,
    TaskExecutionLogDetailSerializer,
    TaskExecutionLogSerializer,
)
from .tasks import dispatch


class CredentialViewSet(viewsets.ModelViewSet):
    """连接凭据管理（仅管理员）。"""

    queryset = Credential.objects.all()
    serializer_class = CredentialSerializer
    permission_classes = [IsAdmin]


class ExecutionTaskViewSet(viewsets.ModelViewSet):
    """任务执行：创建即调度执行；普通用户只读，管理员可创建/重跑。"""

    queryset = ExecutionTask.objects.select_related('credential', 'created_by').prefetch_related('servers')
    serializer_class = ExecutionTaskSerializer
    permission_classes = [IsAdminOrReadOnly]
    http_method_names = ['get', 'post', 'delete', 'head', 'options']

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        task_type = params.get('task_type')
        status = params.get('status')
        keyword = params.get('keyword')
        if task_type:
            qs = qs.filter(task_type=task_type)
        if status:
            qs = qs.filter(status=status)
        if keyword:
            qs = qs.filter(name__icontains=keyword)
        return qs

    def perform_create(self, serializer):
        task = serializer.save(created_by=self.request.user, status='pending')
        dispatch(task, triggered_by=self.request.user, trigger_type='create')

    @action(detail=True, methods=['post'])
    def rerun(self, request, pk=None):
        task = self.get_object()
        if task.status == 'running':
            return fail('任务正在执行中，无法重跑')
        task.status = 'pending'
        task.return_code = None
        task.output = ''
        task.started_at = None
        task.finished_at = None
        task.save(update_fields=['status', 'return_code', 'output', 'started_at', 'finished_at'])
        dispatch(task, triggered_by=request.user, trigger_type='rerun')
        return ok(ExecutionTaskSerializer(task).data, msg='已重新调度执行')

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
