"""Agent 管理 API（仅管理员）。

- Agent CRUD：创建/重置令牌时明文 token 仅在响应中出现一次；
- 删除保护：有未终态任务单元的 agent 禁止删除；
- jobs 动作：该 agent 最近任务单元列表（排障）。
"""
from rest_framework import viewsets
from rest_framework.decorators import action

from common.permissions import IsAdmin
from common.response import fail, ok

from .models import AgentJob, AgentNode
from .serializers import AgentJobSerializer, AgentNodeSerializer

JOBS_LIMIT = 50


class AgentNodeViewSet(viewsets.ModelViewSet):
    queryset = AgentNode.objects.all()
    serializer_class = AgentNodeSerializer
    permission_classes = [IsAdmin]
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
