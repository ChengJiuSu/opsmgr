from rest_framework import serializers

from .executors import get_executor
from .models import Method


class MethodSerializer(serializers.ModelSerializer):
    provider_display = serializers.CharField(source='get_provider_display', read_only=True)
    service_type_name = serializers.CharField(source='service_type.name', read_only=True, default=None)

    class Meta:
        model = Method
        fields = [
            'id', 'name', 'code', 'provider', 'provider_display', 'description',
            'service_type', 'service_type_name',
            'config', 'params_schema', 'enabled', 'created_at', 'updated_at',
        ]
        read_only_fields = ['created_at', 'updated_at']

    def validate(self, attrs):
        provider = attrs.get('provider', getattr(self.instance, 'provider', None))
        config = attrs.get('config', getattr(self.instance, 'config', {}) or {})
        try:
            get_executor(provider).validate_config(config)
        except KeyError:
            raise serializers.ValidationError({'provider': f'未知 provider：{provider}'})
        except ValueError as exc:
            raise serializers.ValidationError({'config': str(exc)})
        return attrs
