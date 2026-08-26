"""Agent 管理 API（仅管理员）。

- Agent CRUD：创建/重置令牌时明文 token 仅在响应中出现一次；
- 删除保护：有未终态任务单元的 agent 禁止删除；
- jobs 动作：该 agent 最近任务单元列表（排障）；
- 通道路由 CRUD + preview：建规则前先看影响哪些主机。
"""
from rest_framework import viewsets
from rest_framework.decorators import action

from common.permissions import IsAdmin
from common.response import fail, ok
from common.sudo import SudoRequired

from .models import AgentJob, AgentNode, AgentRoute
from .serializers import AgentJobSerializer, AgentNodeSerializer, AgentRouteSerializer

JOBS_LIMIT = 50


class AgentNodeViewSet(viewsets.ModelViewSet):
    queryset = AgentNode.objects.all()
    serializer_class = AgentNodeSerializer
    # 配置面写操作要求 sudo 二次验证（common/sudo.py）：改了就能拿到更多东西，
    # 而登录那次 MFA 可能是 60 分钟前的事
    # 建 agent 会发出一次性 token，重置 token 能顶掉现有 agent 连接
    permission_classes = [IsAdmin, SudoRequired]
    pagination_class = None  # agent 数量极少，直接全量返回

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        raw, token_hash = AgentNode.generate_token()
        node = serializer.save(token_hash=token_hash)
        data = self.get_serializer(node).data
        data['token'] = raw  # 明文仅此一次，库中只存哈希
        return ok(data, msg='创建成功，请立即保存 token（不再展示）。')

    def destroy(self, request, *args, **kwargs):
        node = self.get_object()
        if node.jobs.exclude(status__in=AgentJob.TERMINAL_STATUSES).exists():
            return fail('该 Agent 存在未完成的任务单元，禁止删除（可先禁用）。')
        node.delete()
        return ok(msg='已删除。')

    @action(detail=True, methods=['post'])
    def regen_token(self, request, pk=None):
        """重置令牌：旧 token 立即失效，agent 需换新 token 重连。"""
        node = self.get_object()
        raw, token_hash = AgentNode.generate_token()
        node.token_hash = token_hash
        node.save(update_fields=['token_hash', 'updated_at'])
        return ok({'token': raw}, msg='令牌已重置，请立即保存（不再展示），并更新 agent 配置。')

    @action(detail=True, methods=['get'])
    def jobs(self, request, pk=None):
        """该 agent 最近任务单元（排障用，固定最近 50 条）。"""
        qs = self.get_object().jobs.all()[:JOBS_LIMIT]
        return ok(AgentJobSerializer(qs, many=True).data)


class AgentRouteViewSet(viewsets.ModelViewSet):
    """通道路由规则：项目组（必填）+ 环境（可空 = 该项目组默认）→ agent。"""

    queryset = AgentRoute.objects.select_related('agent', 'project_group', 'environment')
    serializer_class = AgentRouteSerializer
    # 配置面写操作要求 sudo 二次验证（common/sudo.py）：改了就能拿到更多东西，
    # 而登录那次 MFA 可能是 60 分钟前的事
    # 改路由 = 改命令下发到哪台机器执行
    permission_classes = [IsAdmin, SudoRequired]
    pagination_class = None  # 规则条数与项目组同量级，全量返回便于一眼看完

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        for key in ('agent', 'project_group', 'environment'):
            value = params.get(key)
            if value:
                qs = qs.filter(**{f'{key}_id': value})
        return qs

    @action(detail=False, methods=['get'])
    def preview(self, request):
        """规则影响面预览：该维度下会被规则接管的主机（channel_mode=auto 才受影响）。"""
        from apps.asset_mgr.models import Server

        project_group = request.query_params.get('project_group')
        if not project_group:
            return fail('缺少 project_group')
        qs = Server.objects.filter(project_group_id=project_group, channel_mode='auto')
        environment = request.query_params.get('environment')
        if environment:
            qs = qs.filter(environment_id=environment)
        hosts = list(qs.order_by('name').values('id', 'name', 'private_ip', 'public_ip')[:200])
        return ok({'count': qs.count(), 'hosts': hosts})
