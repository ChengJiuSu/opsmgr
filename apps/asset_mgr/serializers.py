from django.db import transaction
from rest_framework import serializers

from .models import Channel, CloudProvider, Environment, ProjectGroup, Server, ServerService, ServiceType


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
    # 主机服务摘要：已规划/已部署/已下线记录列表（视图层需 prefetch services__service_type）
    services = serializers.SerializerMethodField()
    # 新增主机时可选分配服务类型：直接创建 planned 记录并自动生成进程名（仅创建时生效）
    service_type_ids = serializers.PrimaryKeyRelatedField(
        queryset=ServiceType.objects.filter(enabled=True), many=True,
        write_only=True, required=False,
    )

    class Meta:
        model = Server
        fields = '__all__'

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
