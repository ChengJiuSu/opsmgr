"""Agent 节点与任务单元序列化器。token 明文只在创建/重置响应中出现一次。"""
from rest_framework import serializers

from .models import AgentJob, AgentNode


class AgentNodeSerializer(serializers.ModelSerializer):
    running_jobs = serializers.SerializerMethodField()

    class Meta:
        model = AgentNode
        fields = [
            'id', 'name', 'enabled', 'status', 'last_heartbeat', 'version',
            'meta', 'remark', 'running_jobs', 'created_at', 'updated_at',
        ]
        read_only_fields = ['status', 'last_heartbeat', 'version', 'meta']

    def get_running_jobs(self, obj):
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
