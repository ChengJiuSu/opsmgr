from django.db import transaction
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from rest_framework.validators import UniqueValidator

from apps.agent_mgr.channel import SOURCE_DIRECT, SOURCE_LABELS, resolve_channels

from .models import Channel, CloudProvider, Environment, ProjectGroup, Server, ServerService, ServiceType

class ServerServiceBriefSerializer(serializers.Serializer):
    """只为 schema 存在：ServerSerializer.get_services 是手拼的 dict 列表，
    没有这个描述的话 spectacular 猜不出类型、整个字段按 string 写进文档。
    字段与 get_services 的返回逐项对应，改一边要改两边。
    """

    id = serializers.IntegerField(help_text='ServerService 记录 ID')
    service_type = serializers.IntegerField(help_text='服务类型 ID')
    service_type_name = serializers.CharField()
    service_type_code = serializers.CharField()
    status = serializers.CharField(help_text='规划 / 部署 / 运行 三态')
    process_name = serializers.CharField()
    process_count = serializers.IntegerField()


# 服务器录入必填的归属维度：字段 → 展示名（云厂商/地域仍可留空）
REQUIRED_DIMS = (
    ('project_group', '项目组'),
    ('channel', '渠道'),
    ('environment', '环境'),
)


class ProjectGroupSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectGroup
        fields = '__all__'


class ChannelSerializer(serializers.ModelSerializer):
    class Meta:
        model = Channel
        fields = '__all__'


class EnvironmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Environment
        fields = '__all__'


class CloudProviderSerializer(serializers.ModelSerializer):
    class Meta:
        model = CloudProvider
        fields = '__all__'


class ServiceTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = ServiceType
        fields = '__all__'


class ServerSerializer(serializers.ModelSerializer):
    # 字典外键的只读展示字段（外键为空时返回 null）
    project_group_name = serializers.CharField(source='project_group.name', read_only=True, default=None)
    channel_name = serializers.CharField(source='channel.name', read_only=True, default=None)
    environment_name = serializers.CharField(source='environment.name', read_only=True, default=None)
    environment_code = serializers.CharField(source='environment.code', read_only=True, default=None)
    environment_is_prod = serializers.BooleanField(source='environment.is_prod', read_only=True, default=None)
    cloud_provider_name = serializers.CharField(source='cloud_provider.name', read_only=True, default=None)
    # 执行通道展示：agent_name 是主机显式指定的目标（channel_mode='agent' 时才生效），
    # effective_* 是解析后的实际通道（含路由规则命中），列表页展示看后者。
    agent_name = serializers.CharField(source='agent.name', read_only=True, default=None)
    effective_agent = serializers.SerializerMethodField()
    effective_agent_name = serializers.SerializerMethodField()
    channel_source = serializers.SerializerMethodField()
    channel_source_label = serializers.SerializerMethodField()
    # 主机服务摘要：已规划/已部署/已下线记录列表（视图层需 prefetch services__service_type）
    services = serializers.SerializerMethodField()
    # 新增主机时可选分配服务类型：直接创建 planned 记录并自动生成进程名（仅创建时生效）
    service_type_ids = serializers.PrimaryKeyRelatedField(
        queryset=ServiceType.objects.filter(enabled=True), many=True,
        write_only=True, required=False,
    )
    # 覆盖默认 UniqueValidator 报错文案
    name = serializers.CharField(
        max_length=100,
        validators=[UniqueValidator(
            queryset=Server.objects.all(), message='主机名已存在，主机名必须全局唯一',
        )],
    )

    class Meta:
        model = Server
        fields = '__all__'

    def to_internal_value(self, data):
        # 前端公网 IP 留空提交空串，统一转 null（NAT 出局主机无公网 IP）
        if isinstance(data, dict) and data.get('public_ip') == '':
            data = {**data, 'public_ip': None}
        return super().to_internal_value(data)

    def validate_public_ip(self, value):
        # 部分唯一约束（有值时唯一）DRF 不会自动校验，手动查重转友好报错
        if value:
            qs = Server.objects.filter(public_ip=value)
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError('该公网 IP 已被其他主机使用')
        return value

    def validate(self, attrs):
        # 归属三维度（项目组/渠道/环境）是录入硬门槛：变量注入、制品匹配、数据范围
        # 都按这三维度定位，缺维度的主机进台账就是一台谁都算不准的「野机器」。
        # 新增必须选齐；编辑允许不传（兼容存量主机），但禁止把已有值清空。
        dim_errors = {}
        for field, label in REQUIRED_DIMS:
            if self.instance is None:
                if attrs.get(field) is None:
                    dim_errors[field] = f'新增服务器必须选择{label}'
            elif field in attrs and attrs[field] is None:
                dim_errors[field] = f'{label}不允许清空'
        if dim_errors:
            raise serializers.ValidationError(dim_errors)

        # 执行通道一致性：仅「指定 Agent」模式保留 agent，其余模式清空，
        # 避免库里留下不生效却看着像生效的残留配置。
        mode = attrs.get('channel_mode', getattr(self.instance, 'channel_mode', 'auto'))
        agent = attrs.get('agent', getattr(self.instance, 'agent', None))
        # 兼容只传 agent 不传 channel_mode 的旧客户端：视为钉死该 agent，
        # 否则下面的清理分支会把选中的 agent 静默丢掉。
        if 'channel_mode' not in attrs and attrs.get('agent') is not None:
            attrs['channel_mode'] = mode = 'agent'
        if mode == 'agent' and agent is None:
            raise serializers.ValidationError({'agent': '执行通道选择「指定 Agent」时必须选择 Agent'})
        if mode != 'agent':
            attrs['agent'] = None
        return attrs

    def _channel(self, obj):
        """解析后的 (agent, source)：列表序列化时一次性批量解析并缓存。

        逐条解析会退化成 N+1，故用 ListSerializer 的 instance 做批量入口。
        """
        cache = self.context.setdefault('_channel_cache', {})
        if obj.pk not in cache:
            batch = self.parent.instance if isinstance(self.parent, serializers.ListSerializer) else None
            cache.update(resolve_channels(batch if batch is not None else [obj]))
        return cache.get(obj.pk, (None, SOURCE_DIRECT))

    def get_effective_agent(self, obj) -> int | None:
        agent = self._channel(obj)[0]
        return agent.pk if agent else None

    def get_effective_agent_name(self, obj) -> str | None:
        agent = self._channel(obj)[0]
        return agent.name if agent else None

    def get_channel_source(self, obj) -> str:
        return self._channel(obj)[1]

    def get_channel_source_label(self, obj) -> str:
        return SOURCE_LABELS[self._channel(obj)[1]]

    @extend_schema_field(ServerServiceBriefSerializer(many=True))
    def get_services(self, obj):
        return [
            {
                'id': link.id,
                'service_type': link.service_type_id,
                'service_type_name': link.service_type.name,
                'service_type_code': link.service_type.code,
                'status': link.status,
                'process_name': link.process_name,
                'process_count': link.process_count,
            }
            for link in obj.services.all()
        ]

    def create(self, validated_data):
        service_types = validated_data.pop('service_type_ids', [])
        with transaction.atomic():
            server = super().create(validated_data)
            if service_types:
                links = ServerService.build_planned_links(
                    [(server, st) for st in service_types],
                )
                ServerService.objects.bulk_create(links)
        return server

    def update(self, instance, validated_data):
        # 服务规划的调整走「服务器分配」页，编辑主机时忽略该字段
        validated_data.pop('service_type_ids', None)
        return super().update(instance, validated_data)
