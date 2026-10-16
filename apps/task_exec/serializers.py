import re

import yaml
from django.db import transaction
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .models import (
    Credential,
    ExecutionTask,
    ExecVariable,
    ExecVariableVersion,
    TaskExecutionLog,
    TaskStep,
    snapshot_variable,
)

VARIABLE_KEY_RE = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')


class ExecVariableSerializer(serializers.ModelSerializer):
    """执行变量序列化器：key 须为合法 ansible 变量名，值须为合法 YAML，
    生效维度三选必填；每次保存落一条版本快照。

    敏感变量（is_secret）：值加密存储且**任何读接口都不回显**（给 has_value
    布尔），要看真值走 reveal 动作（管理员 + sudo，打审计）。校验也跳过 YAML
    ——敏感值按字面字符串注入，密码里的 `#` 不该被当注释。
    """

    project_group_name = serializers.CharField(source='project_group.name', read_only=True, default=None)
    channel_name = serializers.CharField(source='channel.name', read_only=True, default=None)
    environment_name = serializers.CharField(source='environment.name', read_only=True, default=None)
    has_value = serializers.SerializerMethodField()

    class Meta:
        model = ExecVariable
        fields = [
            'id', 'key', 'value', 'is_secret', 'has_value', 'description',
            'project_group', 'project_group_name', 'channel', 'channel_name',
            'environment', 'environment_name',
            'enabled', 'created_at', 'updated_at',
        ]

    @extend_schema_field(OpenApiTypes.BOOL)
    def get_has_value(self, obj):
        return bool(obj.value)

    def to_representation(self, instance):
        """敏感变量不回显值。

        放 to_representation 而不是把 value 设成 write_only：普通变量的值页面上
        要正常显示与编辑，只有敏感的那些要藏。
        """
        data = super().to_representation(instance)
        if instance.is_secret:
            data['value'] = ''
        return data

    def validate_key(self, value):
        if not VARIABLE_KEY_RE.match(value):
            raise serializers.ValidationError('变量名须以字母/下划线开头，仅含字母、数字、下划线')
        return value


    def validate(self, attrs):
        # 生效条件三维度必选：执行时按任务/模块有效维度精确匹配加载
        for field, label in (('project_group', '项目组'), ('channel', '渠道'), ('environment', '环境')):
            value = attrs.get(field, getattr(self.instance, field, None) if self.instance else None)
            if value is None:
                raise serializers.ValidationError({field: f'必须选择{label}（变量生效条件三维度必选）'})
        self._validate_value(attrs)
        return attrs

    def _validate_value(self, attrs):
        """值校验按 is_secret 分流（不能放 validate_value：那时拿不到 is_secret）。"""
        is_secret = attrs.get(
            'is_secret', getattr(self.instance, 'is_secret', False) if self.instance else False)
        value = attrs.get('value')
        # 敏感值不过 YAML 校验：按字面字符串注入，`#`/`:` 不该被 YAML 解释
        if not is_secret and (value or '').strip():
            try:
                yaml.safe_load(value)
            except yaml.YAMLError as exc:
                raise serializers.ValidationError({'value': f'变量值不是合法 YAML：{exc}'})
        # 新建敏感变量必须给值：留空会产出「标记为敏感但其实是空」的变量，
        # 执行时注入的是空密码
        if is_secret and self.instance is None and not (value or '').strip():
            raise serializers.ValidationError({'value': '敏感变量必须填写变量值'})

    def _user(self):
        request = self.context.get('request')
        return getattr(request, 'user', None)

    @transaction.atomic
    def create(self, validated_data):
        raw = validated_data.pop('value', '')
        variable = ExecVariable(**validated_data)
        variable.set_value(raw)
        variable.save()
        snapshot_variable(variable, user=self._user())
        return variable

    @transaction.atomic
    def update(self, instance, validated_data):
        """更新值/敏感标记。

        三种情况都要正确编码，否则库里会留下「标记与内容不一致」的值：
        - 传了新值 → 按新的 is_secret 编码；
        - 只切 is_secret（不传值）→ 拿现有明文重新编码（明文→加密 / 密文→解开）；
        - 都没传 → 原样不动。
        """
        has_new_value = 'value' in validated_data
        raw = validated_data.pop('value', None)
        switching = 'is_secret' in validated_data \
            and validated_data['is_secret'] != instance.is_secret
        if switching and not has_new_value:
            raw, has_new_value = instance.get_value(), True
        for field, value in validated_data.items():
            setattr(instance, field, value)
        if has_new_value:
            instance.set_value(raw)
        instance.save()
        snapshot_variable(instance, user=self._user())
        return instance


class ExecVariableVersionSerializer(serializers.ModelSerializer):
    """变量版本序列化器：历史快照列表与回滚数据源。

    敏感版本同样不回显值——否则「当前值藏起来、翻一下历史就看到了」。
    """

    created_by_name = serializers.CharField(source='created_by.username', read_only=True, default='')
    project_group_name = serializers.CharField(source='project_group.name', read_only=True, default=None)
    channel_name = serializers.CharField(source='channel.name', read_only=True, default=None)
    environment_name = serializers.CharField(source='environment.name', read_only=True, default=None)

    class Meta:
        model = ExecVariableVersion
        fields = [
            'id', 'version', 'value', 'is_secret', 'description', 'enabled',
            'project_group_name', 'channel_name', 'environment_name',
            'created_by_name', 'created_at',
        ]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if instance.is_secret:
            data['value'] = ''
        return data


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
        names = '、'.join(f'{s.name}({s.conn_ip})' for s in missing[:5])
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
    service_type_name = serializers.CharField(source='service_type.name', read_only=True, default=None)

    class Meta:
        model = TaskStep
        fields = [
            'id', 'order', 'name', 'task_type', 'task_type_display',
            'command', 'playbook', 'method', 'method_name', 'tags', 'extra_vars', 'params', 'timeout',
            'run_parallel',
            'use_custom_scope', 'project_group', 'project_group_name',
            'channel', 'channel_name', 'environment', 'environment_name', 'environment_is_prod',
            'service_type', 'service_type_name',
        ]

    def validate_params(self, value):
        if value in (None, ''):
            return {}
        if not isinstance(value, dict):
            raise serializers.ValidationError('方法参数必须为 JSON 对象（如 {"key": "value"}）')
        return value

    def validate(self, attrs):
        task_type = attrs.get('task_type', 'command')
        name = attrs.get('name') or ''
        label = f'模块「{name}」' if name else '模块'
        if task_type == 'command' and not (attrs.get('command') or '').strip():
            raise serializers.ValidationError({'command': f'{label}为命令类型，必须填写 Shell 命令'})
        if task_type == 'playbook':
            if not (attrs.get('playbook') or '').strip():
                raise serializers.ValidationError({'playbook': f'{label}为 Playbook 类型，必须选择剧本方法'})
            # tags 必选：不允许留空执行全部
            if not (attrs.get('tags') or '').strip():
                raise serializers.ValidationError({'tags': f'{label}必须选择一个 tags（不支持留空执行全部）'})
        if task_type == 'method' and attrs.get('method') is None:
            raise serializers.ValidationError({'method': f'{label}为平台方法类型，必须选择一个方法'})
        # 暂停确认必须独立成阶段：并行组内暂停会让组内其他模块空等，语义不成立
        if task_type == 'pause' and attrs.get('run_parallel'):
            raise serializers.ValidationError({'run_parallel': f'{label}为暂停确认类型，不允许标记并行'})
        # 暂停确认不涉及主机，服务类型过滤无意义
        if task_type == 'pause' and attrs.get('service_type'):
            raise serializers.ValidationError({'service_type': f'{label}为暂停确认类型，不涉及主机，不能选服务类型'})
        # 其余模块服务类型必选：不再支持「不限」，未选不予保存
        if task_type != 'pause' and attrs.get('service_type') is None:
            raise serializers.ValidationError({'service_type': f'{label}必须选择服务类型（不支持不限）'})
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
    # 仅 pending 时有值：排第几、前面是谁、是否在等主机锁（见 exec_queue.py）
    queue = serializers.SerializerMethodField()

    class Meta:
        model = ExecutionTask
        fields = [
            'id', 'name', 'steps',
            'project_group', 'project_group_name', 'channel', 'channel_name',
            'environment', 'environment_name', 'environment_is_prod',
            'exec_mode', 'exec_mode_display', 'forks', 'default_select_all', 'im_enabled', 'notify_enabled', 'servers',
            'credential', 'credential_name',
            'status', 'status_display', 'pause_type', 'resume_deadline', 'return_code', 'output', 'queue',
            'created_by_name', 'created_at', 'started_at', 'finished_at', 'duration',
        ]
        read_only_fields = [
            'status', 'pause_type', 'resume_deadline', 'return_code', 'output', 'created_at',
            'started_at', 'finished_at',
        ]

    @extend_schema_field(OpenApiTypes.OBJECT)
    def get_servers(self, obj):
        return [{'id': s.id, 'name': s.name, 'ip': s.conn_ip} for s in obj.servers.all()]

    @extend_schema_field(OpenApiTypes.OBJECT)
    def get_queue(self, obj):
        from .exec_queue import queue_info
        return queue_info(obj)

    def validate_steps(self, value):
        if not value:
            raise serializers.ValidationError('任务流至少需要一个任务模块')
        return value

    def validate_forks(self, value):
        if not 1 <= value <= 200:
            raise serializers.ValidationError('主机并发数须在 1-200 之间')
        return value

    def validate(self, attrs):
        # 主机范围三维度必选（不再支持留空=不限），未选齐不予保存/执行
        for field, label in (('project_group', '项目组'), ('channel', '渠道'), ('environment', '环境')):
            value = attrs.get(field, getattr(self.instance, field, None) if self.instance else None)
            if value is None:
                raise serializers.ValidationError({field: f'必须选择{label}（主机范围三维度必选）'})

        # 飞书里没法像执行弹窗那样逐模块勾主机：关掉默认全选的任务本就是要求
        # 人工确认，开了飞书执行等于把这条要求绕过去
        def current(field):
            """字段的「本次保存后」取值。

            新建时未提交的布尔字段**不在 attrs 里**（模型默认值要到 save 才生效），
            直接取 None 会把「没传 default_select_all」误判成关闭状态，于是新建
            带 im_enabled 的任务一律被拒。取模型默认值兜底。
            """
            if field in attrs:
                return attrs[field]
            if self.instance is not None:
                return getattr(self.instance, field)
            return ExecutionTask._meta.get_field(field).default

        if current('im_enabled') and not current('default_select_all'):
            raise serializers.ValidationError({
                'im_enabled': '关闭「执行时默认全选主机」的任务不支持飞书执行'
                              '（飞书里无法逐模块勾选主机）',
            })
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


class ExecutionTaskListSerializer(ExecutionTaskSerializer):
    """列表序列化器：与详情同结构，但**不含 output**。

    output 是执行日志的尾部预览，上限 live.DB_PREVIEW_LIMIT。列表页在有任务
    执行中时每 5 秒轮询一次 page_size=20，带上它每次都要多序列化并传一遍 20 份
    预览 —— 而列表根本不展示日志，实时日志走 tasks/<id>/output/ 那个增量接口。
    """

    class Meta(ExecutionTaskSerializer.Meta):
        fields = [f for f in ExecutionTaskSerializer.Meta.fields if f != 'output']


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
        # 执行时完整日志流式落盘；文件过保留期被清理后置空，此处即为 false
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
