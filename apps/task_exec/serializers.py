import re

from django.db import transaction
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .models import Credential, ExecutionTask, ExecVariable, TaskExecutionLog, TaskStep

VARIABLE_KEY_RE = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')


class ExecVariableSerializer(serializers.ModelSerializer):
    """执行变量序列化器：key 须为合法 ansible 变量名。"""

    class Meta:
        model = ExecVariable
        fields = ['id', 'key', 'value', 'description', 'enabled', 'created_at', 'updated_at']

    def validate_key(self, value):
        if not VARIABLE_KEY_RE.match(value):
            raise serializers.ValidationError('变量名须以字母/下划线开头，仅含字母、数字、下划线')
        return value


class CredentialSerializer(serializers.ModelSerializer):
    """凭据序列化器。敏感字段只写不回显，是否已设置以布尔标志返回。"""

    password = serializers.CharField(write_only=True, required=False, allow_blank=True)
    private_key = serializers.CharField(write_only=True, required=False, allow_blank=True)
    become_password = serializers.CharField(write_only=True, required=False, allow_blank=True)
    has_password = serializers.SerializerMethodField()
    has_private_key = serializers.SerializerMethodField()

    class Meta:
        model = Credential
        fields = [
            'id', 'name', 'username', 'auth_type', 'password', 'private_key',
            'become', 'become_password', 'has_password', 'has_private_key',
            'created_at', 'updated_at',
        ]

    @extend_schema_field(OpenApiTypes.BOOL)
    def get_has_password(self, obj):
        return bool(obj.password)

    @extend_schema_field(OpenApiTypes.BOOL)
    def get_has_private_key(self, obj):
        return bool(obj.private_key)

    def update(self, instance, validated_data):
        # 空字符串视为「不修改」，避免清空已存凭据
        for secret in ('password', 'private_key', 'become_password'):
            if validated_data.get(secret) == '':
                validated_data.pop(secret)
        return super().update(instance, validated_data)


def validate_service_planned(method, servers):
    """部署前置校验：方法关联了服务类型时，目标主机必须已规划/已部署该服务。

    返回错误文案（None 表示通过），由执行入口（views.execute）调用。
    """
    from apps.asset_mgr.models import ServerService

    if not method or not method.service_type_id or not servers:
        return None
    planned_ids = set(
        ServerService.objects.filter(
            server__in=servers,
            service_type_id=method.service_type_id,
            status__in=('planned', 'deployed'),
        ).values_list('server_id', flat=True)
    )
    missing = [s for s in servers if s.id not in planned_ids]
    if missing:
        names = '、'.join(f'{s.name}({s.ip})' for s in missing[:5])
        more = f' 等 {len(missing)} 台' if len(missing) > 5 else ''
        return (
            f'主机 {names}{more}未规划服务「{method.service_type.name}」，'
            '请先在「配置管理 → 服务器分配」完成服务类型规划'
        )
    return None


class TaskStepSerializer(serializers.ModelSerializer):
    """任务模块序列化器（嵌套在任务内读写）。"""

    method_name = serializers.CharField(source='method.name', read_only=True, default=None)
    task_type_display = serializers.CharField(source='get_task_type_display', read_only=True)
    project_group_name = serializers.CharField(source='project_group.name', read_only=True, default=None)
    channel_name = serializers.CharField(source='channel.name', read_only=True, default=None)
    environment_name = serializers.CharField(source='environment.name', read_only=True, default=None)
    environment_is_prod = serializers.BooleanField(source='environment.is_prod', read_only=True, default=None)

    class Meta:
        model = TaskStep
        fields = [
            'id', 'order', 'name', 'task_type', 'task_type_display',
            'command', 'playbook', 'method', 'method_name', 'tags', 'extra_vars', 'timeout',
            'use_custom_scope', 'project_group', 'project_group_name',
            'channel', 'channel_name', 'environment', 'environment_name', 'environment_is_prod',
        ]

    def validate(self, attrs):
        task_type = attrs.get('task_type', 'command')
        name = attrs.get('name') or ''
        label = f'模块「{name}」' if name else '模块'
        if task_type == 'command' and not (attrs.get('command') or '').strip():
            raise serializers.ValidationError({'command': f'{label}为命令类型，必须填写 Shell 命令'})
        if task_type == 'playbook' and not (attrs.get('playbook') or '').strip():
            raise serializers.ValidationError({'playbook': f'{label}为 Playbook 类型，必须选择剧本文件'})
        if task_type == 'method' and attrs.get('method') is None:
            raise serializers.ValidationError({'method': f'{label}为平台方法类型，必须选择一个方法'})
        # 自定义主机范围时三维度必选（与任务全局范围同策略，不支持留空=不限）
        if attrs.get('use_custom_scope'):
            for field, dim in (('project_group', '项目组'), ('channel', '渠道'), ('environment', '环境')):
                if attrs.get(field) is None:
                    raise serializers.ValidationError({field: f'{label}自定义主机范围必须选择{dim}'})
        return attrs


class ExecutionTaskSerializer(serializers.ModelSerializer):
    """任务（任务流）序列化器：嵌套模块整体读写，主机相关校验移至执行入口。"""

    steps = TaskStepSerializer(many=True)
    servers = serializers.SerializerMethodField(read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    credential_name = serializers.CharField(source='credential.name', read_only=True)
    project_group_name = serializers.CharField(source='project_group.name', read_only=True, default=None)
    channel_name = serializers.CharField(source='channel.name', read_only=True, default=None)
    environment_name = serializers.CharField(source='environment.name', read_only=True, default=None)
    environment_is_prod = serializers.BooleanField(source='environment.is_prod', read_only=True, default=None)
    exec_mode_display = serializers.CharField(source='get_exec_mode_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    duration = serializers.FloatField(read_only=True, allow_null=True)

    class Meta:
        model = ExecutionTask
        fields = [
            'id', 'name', 'steps',
            'project_group', 'project_group_name', 'channel', 'channel_name',
            'environment', 'environment_name', 'environment_is_prod',
            'exec_mode', 'exec_mode_display', 'servers',
            'credential', 'credential_name',
            'status', 'status_display', 'return_code', 'output',
            'created_by_name', 'created_at', 'started_at', 'finished_at', 'duration',
        ]
        read_only_fields = [
            'status', 'return_code', 'output', 'created_at',
            'started_at', 'finished_at',
        ]

    @extend_schema_field(OpenApiTypes.OBJECT)
    def get_servers(self, obj):
        return [{'id': s.id, 'name': s.name, 'ip': s.ip} for s in obj.servers.all()]

    def validate_steps(self, value):
        if not value:
            raise serializers.ValidationError('任务流至少需要一个任务模块')
        return value

    def validate(self, attrs):
        # 主机范围三维度必选（不再支持留空=不限），未选齐不予保存/执行
        for field, label in (('project_group', '项目组'), ('channel', '渠道'), ('environment', '环境')):
            value = attrs.get(field, getattr(self.instance, field, None) if self.instance else None)
            if value is None:
                raise serializers.ValidationError({field: f'必须选择{label}（主机范围三维度必选）'})
        return attrs

    def _save_steps(self, task, steps_data):
        """整体替换任务流模块：删除重建，序号按提交顺序归一。"""
        task.steps.all().delete()
        for index, step in enumerate(steps_data, 1):
            step.pop('id', None)
            TaskStep.objects.create(task=task, **{**step, 'order': index})

    @transaction.atomic
    def create(self, validated_data):
        steps_data = validated_data.pop('steps')
        task = super().create(validated_data)
        self._save_steps(task, steps_data)
        return task

    @transaction.atomic
    def update(self, instance, validated_data):
        steps_data = validated_data.pop('steps', None)
        task = super().update(instance, validated_data)
        if steps_data is not None:
            self._save_steps(task, steps_data)
        return task


class TaskExecutionLogSerializer(serializers.ModelSerializer):
    """执行日志列表序列化器：不含输出大字段，用于历史列表。"""

    triggered_by_name = serializers.CharField(source='triggered_by.username', read_only=True, default='')
    trigger_type_display = serializers.CharField(source='get_trigger_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    duration = serializers.FloatField(read_only=True, allow_null=True)
    has_full_log = serializers.SerializerMethodField()
    server_count = serializers.SerializerMethodField()

    class Meta:
        model = TaskExecutionLog
        fields = [
            'id', 'task', 'trigger_type', 'trigger_type_display',
            'triggered_by_name', 'exec_mode', 'server_count', 'steps_result',
            'status', 'status_display', 'return_code',
            'has_full_log', 'created_at', 'started_at', 'finished_at', 'duration',
        ]

    @extend_schema_field(OpenApiTypes.BOOL)
    def get_has_full_log(self, obj):
        # 输出超过预览上限时才落盘完整日志文件
        return bool(obj.log_file)

    @extend_schema_field(OpenApiTypes.INT)
    def get_server_count(self, obj):
        return len(obj.servers_snapshot or [])


class TaskExecutionLogDetailSerializer(TaskExecutionLogSerializer):
    """执行日志详情序列化器：含完整输出（超长部分从落盘文件读取）与主机快照。"""

    output = serializers.SerializerMethodField()

    class Meta(TaskExecutionLogSerializer.Meta):
        fields = TaskExecutionLogSerializer.Meta.fields + ['output', 'servers_snapshot']

    @extend_schema_field(OpenApiTypes.STR)
    def get_output(self, obj):
        from pathlib import Path

        from django.conf import settings

        if obj.log_file:
            path = Path(settings.TASK_LOG_DIR) / obj.log_file
            try:
                return path.read_text(encoding='utf-8')
            except OSError:
                # 文件缺失/不可读时退回数据库预览，并附提示
                return obj.output + '\n\n[完整日志文件读取失败，以上为截断预览]'
        return obj.output
