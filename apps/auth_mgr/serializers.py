import re

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.utils import timezone
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from common.request import parse_ip_rules

from .apikey import HIGH_RISK_SCOPES, SCOPES
from .models import ApiCredential
from .services import AMR_PASSWORD

#: DRF 限速语法：`次数/周期`，周期只看首字母（s/m/h/d），min/hour/day 都合法
RATE_RE = re.compile(r'^\d+/(s|sec|second|m|min|minute|h|hour|d|day)$')

User = get_user_model()


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token['username'] = user.username
        token['department'] = user.department
        token['is_staff'] = user.is_staff
        # 这条路径只过了密码；过了第二因子的令牌由 tokens.issue_after_mfa 签发
        token['amr'] = [AMR_PASSWORD]
        return token


class UserSerializer(serializers.ModelSerializer):
    """用户读写序列化器。创建/更新时可选传入 password。"""

    password = serializers.CharField(
        write_only=True, required=False, allow_blank=True, style={'input_type': 'password'}
    )
    # 只读展示：管理员在用户列表看谁还没绑 MFA（绑定与解绑只能由本人操作，
    # 管理员只能重置，见 UserViewSet.reset_mfa）
    mfa_enabled = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            'id', 'username', 'password', 'email', 'phone', 'department',
            'is_active', 'is_staff', 'is_ldap', 'mfa_enabled',
            'last_login', 'date_joined',
        ]
        read_only_fields = ['id', 'last_login', 'date_joined', 'is_ldap', 'mfa_enabled']

    def get_mfa_enabled(self, obj):
        mfa = getattr(obj, 'mfa', None)
        return bool(mfa and mfa.enabled)

    def validate_password(self, value):
        if value:
            validate_password(value)
        return value

    def create(self, validated_data):
        password = validated_data.pop('password', None)
        user = User(**validated_data)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save()
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop('password', None)
        user = super().update(instance, validated_data)
        if password:
            user.set_password(password)
            user.save(update_fields=['password'])
        return user


class ChangePasswordSerializer(serializers.Serializer):
    old_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True)

    def validate_old_password(self, value):
        user = self.context['request'].user
        if not user.check_password(value):
            raise serializers.ValidationError('原密码不正确')
        return value

    def validate_new_password(self, value):
        validate_password(value, self.context['request'].user)
        return value

    def save(self, **kwargs):
        user = self.context['request'].user
        user.set_password(self.validated_data['new_password'])
        user.save(update_fields=['password'])
        return user


class MfaVerifySerializer(serializers.Serializer):
    """登录第二步：用挑战票据 + 验证码（或恢复码）换正式令牌。"""

    mfa_token = serializers.CharField()
    code = serializers.CharField()


class MfaCodeSerializer(serializers.Serializer):
    code = serializers.CharField()


class MfaSensitiveSerializer(serializers.Serializer):
    """解绑 / 重发恢复码：密码 + 当前验证码双确认。

    只要密码不够——会话被劫持的人知道密码的概率不低（钓鱼），但拿不到验证器；
    只要验证码也不够——捡到已解锁手机的人能拿到码。两个都要才算本人。
    """

    password = serializers.CharField()
    code = serializers.CharField()


class SudoSerializer(serializers.Serializer):
    """开 sudo 窗口的入参：二者给一个即可（校验逻辑在视图里按是否启用 MFA 分流）。

    已启用 MFA 的账号必须给 code——密码在钓鱼场景下拿得到，验证器拿不到，
    而 sudo 的意义正是「刚刚证明过是本人」。
    """

    code = serializers.CharField(required=False, allow_blank=True)
    password = serializers.CharField(required=False, allow_blank=True)


class ApiCredentialSerializer(serializers.ModelSerializer):
    """API 凭证读写。

    响应里永远没有 key 明文与签名密钥：明文只在 create/rotate 的响应里单独
    附一次（见 ApiCredentialViewSet），签名密钥只回一个 has_signing_secret 布尔。
    """

    signing_secret = serializers.CharField(
        write_only=True, required=False, allow_blank=True,
        style={'input_type': 'password'}, label='签名密钥',
    )
    has_signing_secret = serializers.BooleanField(read_only=True)
    # 数据范围的可读描述，列表页直接展示（省得前端再拼字典名）
    scope_label = serializers.CharField(read_only=True)
    bound_user_name = serializers.CharField(source='bound_user.username', read_only=True)
    created_by_name = serializers.CharField(
        source='created_by.username', read_only=True, default=None)
    expired = serializers.SerializerMethodField()

    class Meta:
        model = ApiCredential
        fields = [
            'id', 'name', 'key_id', 'scopes',
            'ip_unrestricted', 'ip_whitelist',
            'sig_mode', 'sig_header', 'sig_prefix',
            'signing_secret', 'has_signing_secret',
            'rate_limit', 'bound_user', 'bound_user_name',
            'allowed_project_groups', 'allowed_environments', 'scope_label',
            'enabled', 'expires_at', 'expired',
            'last_used_at', 'last_used_ip',
            'created_by', 'created_by_name', 'remark',
            'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'key_id', 'last_used_at', 'last_used_ip',
            'created_by', 'created_at', 'updated_at',
        ]
        extra_kwargs = {
            # 不传就按凭证名自动建服务账号（见视图 perform_create）
            'bound_user': {'required': False},
        }

    def get_expired(self, obj):
        return obj.is_expired()

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError('凭证名称不能为空。')
        return value

    def validate_scopes(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError('作用域必须是数组。')
        cleaned = [str(item).strip() for item in value if str(item).strip()]
        if not cleaned:
            raise serializers.ValidationError('至少选择一个作用域，否则这把 key 什么都调不了。')
        unknown = [item for item in cleaned if item not in SCOPES]
        if unknown:
            raise serializers.ValidationError(
                f'未知作用域：{"、".join(unknown)}；可选：{"、".join(SCOPES)}。')
        return sorted(set(cleaned))

    def validate_rate_limit(self, value):
        value = (value or '').strip()
        if not value:
            return ''
        if not RATE_RE.match(value):
            raise serializers.ValidationError('限速格式应为「次数/周期」，周期取 s/m/min/h/day，如 60/min。')
        return value

    def validate_expires_at(self, value):
        if value and value <= timezone.now():
            raise serializers.ValidationError('过期时间必须晚于当前时间。')
        return value

    def validate(self, attrs):
        self._validate_ip(attrs)
        self._validate_signature(attrs)
        return attrs

    def _validate_ip(self, attrs):
        """白名单留空必须显式勾选「不限制」。

        默认拒的理由：留空放行会让「忘了填」与「故意不填」外观一致，一次手滑
        就产出一把全网可用的 key。勾了不限制就把白名单清空，避免页面上留着
        一份看着生效实则被忽略的文本。

        高敏作用域（HIGH_RISK_SCOPES，当前是读敏感变量真值）**禁止**不限制：
        它能拿到密码类内容，泄露后果与其余作用域不在一个量级，这里不给「我知道
        风险」的余地——真要放开就拆一把只带普通作用域的 key。
        """
        unrestricted = self._value(attrs, 'ip_unrestricted', False)
        scopes = self._value(attrs, 'scopes', []) or []
        high_risk = sorted(set(scopes) & HIGH_RISK_SCOPES)
        if unrestricted and high_risk:
            raise serializers.ValidationError({
                'ip_unrestricted': f'作用域 {"、".join(high_risk)} 属高敏，必须限定来源 IP，'
                                   f'不允许「不限制来源 IP」。',
            })
        whitelist = (self._value(attrs, 'ip_whitelist', '') or '').strip()
        if unrestricted:
            attrs['ip_whitelist'] = ''
            return
        if not whitelist:
            raise serializers.ValidationError({
                'ip_whitelist': '必须填写来源 IP 白名单；确实不限制来源请显式勾选「不限制来源 IP」。',
            })
        try:
            parse_ip_rules(whitelist)
        except ValueError as exc:
            raise serializers.ValidationError({'ip_whitelist': f'白名单条目非法：{exc}'}) from exc
        attrs['ip_whitelist'] = whitelist

    def _validate_signature(self, attrs):
        mode = self._value(attrs, 'sig_mode', ApiCredential.SIG_NONE)
        if mode == ApiCredential.SIG_NONE:
            attrs['sig_header'] = ''
            attrs['sig_prefix'] = ''
            return
        header = (self._value(attrs, 'sig_header', '') or '').strip()
        if not header:
            raise serializers.ValidationError({'sig_header': '开启签名校验时必须指定签名请求头。'})
        attrs['sig_header'] = header
        secret = (attrs.get('signing_secret') or '').strip()
        if not secret and not (self.instance and self.instance.has_signing_secret):
            raise serializers.ValidationError({'signing_secret': '开启签名校验时必须设置签名密钥。'})
        if secret and len(secret) < 16:
            raise serializers.ValidationError({'signing_secret': '签名密钥至少 16 个字符。'})

    def _value(self, attrs, field, default):
        """取「本次提交的值 → 实例现值 → 默认值」，供 PATCH 局部更新校验。"""
        if field in attrs:
            return attrs[field]
        if self.instance is not None:
            return getattr(self.instance, field)
        return default

    def create(self, validated_data):
        secret = validated_data.pop('signing_secret', '')
        groups = validated_data.pop('allowed_project_groups', None)
        envs = validated_data.pop('allowed_environments', None)
        credential = ApiCredential(**validated_data)
        if secret:
            credential.set_signing_secret(secret)
        credential.save()
        if groups is not None:
            credential.allowed_project_groups.set(groups)
        if envs is not None:
            credential.allowed_environments.set(envs)
        return credential

    def update(self, instance, validated_data):
        secret = validated_data.pop('signing_secret', None)
        # M2M 不能直接 setattr；未传字段保持原值（PATCH 语义）
        groups = validated_data.pop('allowed_project_groups', None)
        envs = validated_data.pop('allowed_environments', None)
        for field, value in validated_data.items():
            setattr(instance, field, value)
        if secret:
            instance.set_signing_secret(secret)
        elif instance.sig_mode == ApiCredential.SIG_NONE:
            # 关掉验签就把密钥一起清掉，不留着一份没人用的秘密
            instance.signing_secret_enc = ''
        instance.save()
        if groups is not None:
            instance.allowed_project_groups.set(groups)
        if envs is not None:
            instance.allowed_environments.set(envs)
        # 范围改了要让缓存失效，否则同请求内后续判定还用旧值
        instance.__dict__.pop('_dim_cache', None)
        return instance
