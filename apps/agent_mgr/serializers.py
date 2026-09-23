"""Agent 节点、任务单元与通道路由序列化器。token 明文只在创建/重置响应中出现一次。"""
from rest_framework import serializers

from .models import AgentJob, AgentNode, AgentRoute


class AgentNodeSerializer(serializers.ModelSerializer):
    running_jobs = serializers.SerializerMethodField()

    class Meta:
        model = AgentNode
        fields = [
            'id', 'name', 'enabled', 'status', 'last_heartbeat', 'version',
            'meta', 'remark', 'running_jobs', 'created_at', 'updated_at',
        ]
        read_only_fields = ['status', 'last_heartbeat', 'version', 'meta']

    def get_running_jobs(self, obj) -> int:
        return obj.jobs.filter(status__in=('sent', 'running')).count()

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError('名称不能为空。')
        return value


class AgentJobSerializer(serializers.ModelSerializer):
    """任务单元列表（排障用）：不含 output 全文，避免列表响应过大。"""
    agent_name = serializers.CharField(source='agent.name', read_only=True)

    class Meta:
        model = AgentJob
        fields = [
            'id', 'job_id', 'agent', 'agent_name', 'task_id', 'log_id',
            'step_order', 'status', 'return_code', 'chunk_seq', 'error',
            'created_at', 'updated_at',
        ]


class AgentRouteSerializer(serializers.ModelSerializer):
    """通道路由规则。唯一冲突转中文报错，不让 IntegrityError 冒成 500。"""

    agent_name = serializers.CharField(source='agent.name', read_only=True)
    project_group_name = serializers.CharField(source='project_group.name', read_only=True)
    environment_name = serializers.CharField(
        source='environment.name', read_only=True, default=None)

    class Meta:
        model = AgentRoute
        fields = [
            'id', 'agent', 'agent_name', 'project_group', 'project_group_name',
            'environment', 'environment_name', 'enabled', 'remark',
            'created_at', 'updated_at',
        ]

    def validate(self, attrs):
        project_group = attrs.get('project_group', getattr(self.instance, 'project_group', None))
        environment = attrs.get('environment', getattr(self.instance, 'environment', None))
        qs = AgentRoute.objects.filter(project_group=project_group, environment=environment)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            scope = f'项目组「{project_group.name}」' + (
                f' + 环境「{environment.name}」' if environment else '（不限环境）')
            raise serializers.ValidationError(f'{scope}已存在通道路由规则，请直接修改该规则。')
        return attrs
