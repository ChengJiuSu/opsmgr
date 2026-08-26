from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from .services import AMR_PASSWORD

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
