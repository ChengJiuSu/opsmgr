from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from . import events, webhook
from .models import NotifyDelivery, NotifyRule, WebhookRobot


class WebhookRobotSerializer(serializers.ModelSerializer):
    """群机器人。token 与签名密钥只写不读，回显打码 / has_xxx。"""

    # 接受 token 或整条 Webhook 地址（管理员多半直接粘贴整条）
    hook_token = serializers.CharField(write_only=True, required=False, allow_blank=True)
    secret = serializers.CharField(write_only=True, required=False, allow_blank=True)
    # 签名密钥「留空 = 不修改」，要去掉签名得显式说
    clear_secret = serializers.BooleanField(write_only=True, required=False, default=False)
    masked_token = serializers.CharField(read_only=True)
    has_secret = serializers.BooleanField(read_only=True)
    rule_count = serializers.SerializerMethodField()

    class Meta:
        model = WebhookRobot
        fields = [
            'id', 'name', 'hook_token', 'masked_token', 'secret', 'has_secret', 'clear_secret',
            'enabled', 'remark', 'rule_count', 'created_at', 'updated_at',
        ]

    @extend_schema_field(OpenApiTypes.INT)
    def get_rule_count(self, obj):
        return obj.rules.count()

    def validate_hook_token(self, value):
        if not str(value or '').strip():
            return ''
        token = webhook.normalize_token(value)
        if token is None:
            raise serializers.ValidationError(
                'Webhook 格式不正确：请填写飞书群机器人的 Webhook 地址或其最后一段 token。')
        return token

    def validate(self, attrs):
        clear = attrs.pop('clear_secret', False)
        if not attrs.get('hook_token'):
            attrs.pop('hook_token', None)
            if self.instance is None:
                raise serializers.ValidationError({'hook_token': '请填写 Webhook 地址或 token。'})
        if clear:
            attrs['secret'] = ''
        elif not attrs.get('secret'):
            attrs.pop('secret', None)
        return attrs


class NotifyRuleSerializer(serializers.ModelSerializer):
    target_type_display = serializers.CharField(source='get_target_type_display', read_only=True)
    target_name = serializers.CharField(read_only=True)
    event_labels = serializers.SerializerMethodField()
    project_group_names = serializers.SerializerMethodField()
    channel_names = serializers.SerializerMethodField()
    environment_names = serializers.SerializerMethodField()

    class Meta:
        model = NotifyRule
        fields = [
            'id', 'name', 'enabled', 'events', 'event_labels',
            'project_groups', 'project_group_names', 'channels', 'channel_names',
            'environments', 'environment_names',
            'target_type', 'target_type_display', 'feishu_chat', 'webhook_robot', 'target_name',
            'mention_trigger', 'remark', 'created_at', 'updated_at',
        ]

    @extend_schema_field(OpenApiTypes.OBJECT)
    def get_event_labels(self, obj):
        return [events.label_of(code) for code in obj.events or []]

    @extend_schema_field(OpenApiTypes.OBJECT)
    def get_project_group_names(self, obj):
        return [item.name for item in obj.project_groups.all()]

    @extend_schema_field(OpenApiTypes.OBJECT)
    def get_channel_names(self, obj):
        return [item.name for item in obj.channels.all()]

    @extend_schema_field(OpenApiTypes.OBJECT)
    def get_environment_names(self, obj):
        return [item.name for item in obj.environments.all()]

    def validate_events(self, value):
        if not isinstance(value, list) or not value:
            raise serializers.ValidationError('至少选择一种通知事件。')
        unknown = [code for code in value if events.get(code) is None]
        if unknown:
            raise serializers.ValidationError(f'未知的通知事件：{"、".join(map(str, unknown))}')
        # 去重且保持顺序
        return list(dict.fromkeys(value))

    def validate(self, attrs):
        current = self.instance
        target_type = attrs.get('target_type', getattr(current, 'target_type', None))
        chat = attrs.get('feishu_chat', getattr(current, 'feishu_chat', None))
        robot = attrs.get('webhook_robot', getattr(current, 'webhook_robot', None))
        # 只保留当前发送方式对应的目标：切换方式后残留的另一个外键会让人误以为两边都在发
        if target_type == NotifyRule.TARGET_FEISHU_CHAT:
            if chat is None:
                raise serializers.ValidationError({'feishu_chat': '请选择应用机器人要发送的会话。'})
            attrs['webhook_robot'] = None
        elif target_type == NotifyRule.TARGET_FEISHU_WEBHOOK:
            if robot is None:
                raise serializers.ValidationError({'webhook_robot': '请选择群机器人。'})
            attrs['feishu_chat'] = None
        else:
            raise serializers.ValidationError({'target_type': '请选择发送方式。'})
        return attrs


class NotifyDeliverySerializer(serializers.ModelSerializer):
    event_code = serializers.CharField(source='event.code', read_only=True)
    event_label = serializers.SerializerMethodField()
    source_type = serializers.CharField(source='event.source_type', read_only=True)
    source_id = serializers.CharField(source='event.source_id', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    target_type_display = serializers.CharField(source='get_target_type_display', read_only=True)

    class Meta:
        model = NotifyDelivery
        fields = [
            'id', 'event_code', 'event_label', 'source_type', 'source_id',
            'rule', 'rule_name', 'target_type', 'target_type_display', 'target_name',
            'title', 'status', 'status_display', 'error', 'created_at', 'sent_at',
        ]

    @extend_schema_field(OpenApiTypes.STR)
    def get_event_label(self, obj):
        return events.label_of(obj.event.code)
