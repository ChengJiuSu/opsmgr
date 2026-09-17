"""飞书配置面的序列化器。

秘密字段一律 `write_only` + `has_xxx` 布尔回显，沿用云账号 / 连接凭据的口径：
配置页要能看出「配没配」，但任何档位都读不到值本身。留空表示不修改——否则
每次改个 IP 白名单都要把三把密钥重新粘一遍，粘错了还查不出来。
"""
from django.contrib.auth import get_user_model
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from common.request import parse_ip_rules

from .models import FeishuApp, FeishuBinding, FeishuChannel

#: 留空表示「不修改」的秘密字段
SECRET_FIELDS = ('app_secret', 'encrypt_key', 'verification_token')


class FeishuAppSerializer(serializers.ModelSerializer):
    """飞书应用配置（单例）。"""

    app_secret = serializers.CharField(write_only=True, required=False, allow_blank=True)
    encrypt_key = serializers.CharField(write_only=True, required=False, allow_blank=True)
    verification_token = serializers.CharField(write_only=True, required=False, allow_blank=True)

    has_app_secret = serializers.BooleanField(read_only=True)
    has_encrypt_key = serializers.BooleanField(read_only=True)
    has_verification_token = serializers.BooleanField(read_only=True)

    callback_slug = serializers.CharField(read_only=True)
    event_url = serializers.SerializerMethodField()
    card_url = serializers.SerializerMethodField()
    missing_fields = serializers.SerializerMethodField()

    class Meta:
        model = FeishuApp
        fields = [
            'app_id', 'app_secret', 'encrypt_key', 'verification_token',
            'has_app_secret', 'has_encrypt_key', 'has_verification_token',
            'callback_slug', 'event_url', 'card_url', 'missing_fields',
            'ip_unrestricted', 'ip_whitelist',
            'enabled', 'sso_enabled', 'sso_redirect_uri', 'auto_create_user',
            'allow_p2p_execute',
            'remark', 'created_at', 'updated_at',
        ]

    @extend_schema_field(OpenApiTypes.STR)
    def get_event_url(self, obj):
        return self._absolute(obj.event_path)

    @extend_schema_field(OpenApiTypes.STR)
    def get_card_url(self, obj):
        return self._absolute(obj.card_path)

    @extend_schema_field(OpenApiTypes.OBJECT)
    def get_missing_fields(self, obj):
        """启用前还缺什么，供页面直接展示，不用管理员自己对照文档猜。"""
        return obj.missing_fields()

    def _absolute(self, path):
        """拼成可直接粘贴到飞书后台的完整地址。

        没有 request（如 shell 调用）时退回相对路径——回调地址是给人看的，
        少了域名比抛异常好。
        """
        request = self.context.get('request')
        return request.build_absolute_uri(path) if request else path

    def validate_ip_whitelist(self, value):
        if str(value or '').strip():
            try:
                parse_ip_rules(value)
            except ValueError as exc:
                raise serializers.ValidationError(f'IP 白名单格式错误：{exc}')
        return value

    def validate(self, attrs):
        # 留空的秘密字段 = 不修改
        for key in SECRET_FIELDS:
            if key in attrs and not attrs[key]:
                attrs.pop(key)

        # 用「合并后的值」判启用条件：只看 attrs 会把「这次没提交但已存库」的
        # 字段误判成缺失，只看 instance 又会漏掉本次刚填上的
        current = self.instance or FeishuApp()
        probe = FeishuApp(
            app_id=attrs.get('app_id', current.app_id),
            app_secret=attrs.get('app_secret', current.app_secret),
            encrypt_key=attrs.get('encrypt_key', current.encrypt_key),
            verification_token=attrs.get(
                'verification_token', current.verification_token),
            ip_unrestricted=attrs.get('ip_unrestricted', current.ip_unrestricted),
            ip_whitelist=attrs.get('ip_whitelist', current.ip_whitelist),
            enabled=attrs.get('enabled', current.enabled),
            sso_enabled=attrs.get('sso_enabled', current.sso_enabled),
        )
        if probe.enabled:
            missing = probe.missing_fields()
            if missing:
                raise serializers.ValidationError(
                    f'启用飞书接入前必须先配置：{"、".join(missing)}')
        if probe.sso_enabled and not probe.enabled:
            raise serializers.ValidationError('启用飞书登录前必须先启用飞书接入。')
        return attrs


class FeishuChannelSerializer(serializers.ModelSerializer):
    """飞书会话白名单。"""

    chat_type_display = serializers.CharField(source='get_chat_type_display', read_only=True)
    notify_scope_display = serializers.CharField(source='get_notify_scope_display', read_only=True)
    notify_project_group_name = serializers.CharField(
        source='notify_project_group.name', read_only=True, default=None)
    notify_environment_name = serializers.CharField(
        source='notify_environment.name', read_only=True, default=None)

    class Meta:
        model = FeishuChannel
        fields = [
            'id', 'chat_id', 'name', 'chat_type', 'chat_type_display',
            'allow_execute', 'notify_enabled', 'enabled', 'remark',
            'notify_scope', 'notify_scope_display',
            'notify_project_group', 'notify_project_group_name',
            'notify_environment', 'notify_environment_name',
            'created_at', 'updated_at',
        ]

    def validate_chat_id(self, value):
        value = str(value or '').strip()
        if not value:
            raise serializers.ValidationError('会话 ID 不能为空。')
        return value

    def validate(self, attrs):
        current = self.instance
        enabled = attrs.get('enabled', getattr(current, 'enabled', True))
        allow_execute = attrs.get(
            'allow_execute', getattr(current, 'allow_execute', False))
        # 停用的会话配「允许执行」只会让人以为这条已经生效，实际一直没生效
        if allow_execute and not enabled:
            raise serializers.ValidationError('已停用的会话不能开启「允许发起任务执行」。')
        return attrs


class FeishuBindingSerializer(serializers.ModelSerializer):
    """飞书绑定关系（管理员维护）。

    `union_id` / `open_id` 由管理员按邮箱手机号反查后带上来，或由飞书登录时
    自动写入。**不提供修改**：改绑等于解绑再绑，两步各自留痕比一次静默改值清楚。
    """

    # 关掉字段级的唯一校验器：它跑在 validate() 之前，报出来的是
    # 「具有 平台用户 的 飞书绑定 已存在」——看不出是谁占着，而管理员最需要
    # 知道的恰恰是这个。唯一性仍由数据库约束兜底，只是报错文案换成人话
    user = serializers.PrimaryKeyRelatedField(
        queryset=get_user_model().objects.all(), validators=[])
    union_id = serializers.CharField(max_length=128, validators=[])

    username = serializers.CharField(source='user.username', read_only=True)
    user_email = serializers.CharField(source='user.email', read_only=True)
    user_is_active = serializers.BooleanField(source='user.is_active', read_only=True)
    source_display = serializers.CharField(source='get_source_display', read_only=True)

    class Meta:
        model = FeishuBinding
        fields = [
            'id', 'user', 'username', 'user_email', 'user_is_active',
            'union_id', 'open_id', 'feishu_name', 'feishu_email',
            'source', 'source_display', 'last_login_at', 'created_at',
        ]
        read_only_fields = ['source', 'last_login_at', 'created_at']

    def validate_union_id(self, value):
        value = str(value or '').strip()
        if not value:
            raise serializers.ValidationError('UnionID 不能为空。')
        return value

    def validate_user(self, value):
        # 服务账号是 API 凭证的行权身份，不是人，绑了也登不进来
        if getattr(value, 'is_service', False):
            raise serializers.ValidationError('服务账号不能绑定飞书。')
        return value

    def validate(self, attrs):
        """把唯一约束翻译成人话。

        默认的报错是「具有 UnionID 的 飞书绑定 已存在」，看不出来是谁占着，
        而管理员最需要知道的恰恰是这个。
        """
        union_id = attrs.get('union_id')
        if union_id:
            taken = FeishuBinding.objects.filter(union_id=union_id).first()
            if taken is not None:
                raise serializers.ValidationError(
                    f'该飞书账号已绑定平台用户「{taken.user.username}」，请先解绑。')
        user = attrs.get('user')
        if user is not None and FeishuBinding.objects.filter(user=user).exists():
            raise serializers.ValidationError(
                f'用户「{user.username}」已绑定飞书账号，请先解绑。')
        return attrs
