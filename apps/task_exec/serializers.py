from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.asset_mgr.models import Server

from .models import Credential, ExecutionTask, TaskExecutionLog


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


class ExecutionTaskSerializer(serializers.ModelSerializer):
    server_ids = serializers.PrimaryKeyRelatedField(
        queryset=Server.objects.all(), many=True, write_only=True, source='servers',
        required=False,
    )
    servers = serializers.SerializerMethodField(read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    credential_name = serializers.CharField(source='credential.name', read_only=True)
    method_name = serializers.CharField(source='method.name', read_only=True)
    method_code = serializers.CharField(source='method.code', read_only=True)
    task_type_display = serializers.CharField(source='get_task_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    duration = serializers.FloatField(read_only=True, allow_null=True)

    class Meta:
        model = ExecutionTask
        fields = [
            'id', 'name', 'task_type', 'task_type_display',
            'command', 'pattern', 'playbook', 'server_ids', 'servers',
            'credential', 'credential_name',
            'method', 'method_name', 'method_code', 'tags', 'limit',
            'extra_vars', 'params',
            'timeout', 'status', 'status_display', 'return_code', 'output',
            'created_by_name', 'created_at', 'started_at', 'finished_at', 'duration',
        ]
        read_only_fields = [
            'status', 'return_code', 'output', 'created_at',
            'started_at', 'finished_at',
        ]

    @extend_schema_field(OpenApiTypes.OBJECT)
    def get_servers(self, obj):
        return [{'id': s.id, 'name': s.name, 'ip': s.ip} for s in obj.servers.all()]

    def validate(self, attrs):
        task_type = attrs.get('task_type', getattr(self.instance, 'task_type', 'command'))
        command = attrs.get('command', getattr(self.instance, 'command', ''))
        playbook = attrs.get('playbook', getattr(self.instance, 'playbook', ''))
        method = attrs.get('method', getattr(self.instance, 'method', None))
        servers = attrs.get('servers', None)
        if task_type == 'command' and not (command or '').strip():
            raise serializers.ValidationError({'command': '命令类型任务必须填写 Shell 命令'})
        if task_type == 'playbook' and not (playbook or '').strip():
            raise serializers.ValidationError({'playbook': 'Playbook 类型任务必须选择剧本文件'})
        if task_type == 'method' and method is None:
            raise serializers.ValidationError({'method': '平台方法任务必须选择一个方法'})
        # 三类任务的执行主机统一来自平台服务器台账，均必选
        if (servers is not None and len(servers) == 0) or (self.instance is None and not servers):
            raise serializers.ValidationError({'server_ids': '必须选择至少一台目标主机'})
        self._validate_servers_planned(servers)
        if task_type == 'method':
            self._validate_service_planned(method, servers)
        return attrs

    @staticmethod
    def _validate_servers_planned(servers):
        """执行权限校验：所选主机必须在服务概要中已规划至少一个服务。

        未规划服务类型（无 planned/deployed 记录）的主机不允许作为执行目标。
        """
        from apps.task_exec.runner import _unplanned_servers

        if not servers:
            return
        unplanned = _unplanned_servers(servers)
        if unplanned:
            names = '、'.join(f'{s.name}({s.ip})' for s in unplanned[:5])
            more = f' 等 {len(unplanned)} 台' if len(unplanned) > 5 else ''
            raise serializers.ValidationError({
                'server_ids': (
                    f'主机 {names}{more}未规划任何服务类型，不允许执行，'
                    '请先在「配置管理 → 服务器分配」完成服务规划'
                ),
            })

    @staticmethod
    def _validate_service_planned(method, servers):
        """部署前置校验：方法关联了服务类型时，目标主机必须已规划/已部署该服务。"""
        from apps.asset_mgr.models import ServerService

        if not method.service_type_id or not servers:
            return
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
            raise serializers.ValidationError({
                'server_ids': (
                    f'主机 {names}{more}未规划服务「{method.service_type.name}」，'
                    '请先在「配置管理 → 服务器分配」完成服务类型规划'
                ),
            })


class TaskExecutionLogSerializer(serializers.ModelSerializer):
    """执行日志列表序列化器：不含输出大字段，用于历史列表。"""

    triggered_by_name = serializers.CharField(source='triggered_by.username', read_only=True, default='')
    trigger_type_display = serializers.CharField(source='get_trigger_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    duration = serializers.FloatField(read_only=True, allow_null=True)
    has_full_log = serializers.SerializerMethodField()

    class Meta:
        model = TaskExecutionLog
        fields = [
            'id', 'task', 'trigger_type', 'trigger_type_display',
            'triggered_by_name', 'status', 'status_display', 'return_code',
            'has_full_log', 'created_at', 'started_at', 'finished_at', 'duration',
        ]

    @extend_schema_field(OpenApiTypes.BOOL)
    def get_has_full_log(self, obj):
        # 输出超过预览上限时才落盘完整日志文件
        return bool(obj.log_file)


class TaskExecutionLogDetailSerializer(TaskExecutionLogSerializer):
    """执行日志详情序列化器：含完整输出（超长部分从落盘文件读取）。"""

    output = serializers.SerializerMethodField()

    class Meta(TaskExecutionLogSerializer.Meta):
        fields = TaskExecutionLogSerializer.Meta.fields + ['output']

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
