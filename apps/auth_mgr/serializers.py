import re

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.utils import timezone
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from common.request import parse_ip_rules

from . import roles
from .apikey import HIGH_RISK_SCOPES, SCOPES
from .models import ApiCredential, Role
from .services import AMR_PASSWORD

#: 角色标识：小写字母、数字、下划线
CODE_RE = re.compile(r'^[a-z][a-z0-9_]*$')

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
        # 权限快照：前端据此决定按钮可见性。**只是少让人点到必然 403 的按钮**，
        # 真正判定始终在后端——快照过期最多多显示一个按钮，点下去照样被拒
        token['perms'] = roles.permission_snapshot(user)
        # 这条路径只过了密码；过了第二因子的令牌由 tokens.issue_after_mfa 签发
        token['amr'] = [AMR_PASSWORD]
        return token


class UserSerializer(serializers.ModelSerializer):
    """用户读写序列化器。创建/更新时可选传入 password。

    role 为空时权限回退 is_staff 语义（见 roles.user_level）：存量账号在角色落地
    前后行为不变，不需要一次性给所有人配角色。
    """

    role_name = serializers.CharField(source='role.name', read_only=True, default='')
    permissions = serializers.SerializerMethodField()
    scope_label = serializers.CharField(read_only=True)

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
            'is_active', 'is_staff', 'is_ldap', 'is_service', 'mfa_enabled',
            'last_login', 'date_joined',
            'role', 'role_name', 'permissions',
            'allowed_project_groups', 'allowed_environments', 'scope_label',
        ]
        read_only_fields = ['id', 'last_login', 'date_joined', 'is_ldap', 'is_service',
                            'mfa_enabled']

    def get_permissions(self, obj):
        """该用户的实际权限快照（含无角色时的回退结果）。"""
        return roles.permission_snapshot(obj)

    def get_mfa_enabled(self, obj):
        mfa = getattr(obj, 'mfa', None)
        return bool(mfa and mfa.enabled)

    def validate_password(self, value):
        if value:
            validate_password(value)
        return value

    def validate(self, attrs):
        """服务账号：不允许设密码，也不允许配角色。

        **密码**——它是 API 凭证的审计身份，密码被置成不可用正是为了「登不进来」。
        给它设了密码就多出一个能从登录页进来、且绕过 MFA 强制范围（非 staff）的
        账号。reset_password 动作那边也拦了一道，这里覆盖 create/update 路径。

        **角色**——机读接口的授权只看**作用域**（apikey.SCOPES）+ IP 白名单 +
        凭证数据范围；`UserOrScope` 按认证方式分流（凭证认证只判作用域），所以
        服务账号身上的角色矩阵对机读调用**不生效**。配得出来但不生效的开关比没有
        这个开关更糟：它会让人以为自己已经把这把 key 收窄了。要收窄机读权限，改
        凭证的作用域或数据范围。

        为什么不改成「作用域 AND 服务账号矩阵」：矩阵比作用域**粗**（artifact:report
        与 artifact:download 同属 artifact 模块，要让上报过就得给 write，而 write
        连删制品都能干），兜不住「作用域给宽了」这种配错；代价却是所有存量 key 因
        为无角色回退到「普通运维」矩阵而当场 403（CI 上报、webhook 触发全挂），
        以及「这把 key 能干什么」要读两个地方。
        """
        if getattr(self.instance, 'is_service', False):
            if attrs.get('password'):
                raise serializers.ValidationError({
                    'password': f'{self.instance.username} 是 API 凭证的服务账号，不允许设置密码。',
                })
            if attrs.get('role') is not None:
                raise serializers.ValidationError({
                    'role': f'{self.instance.username} 是 API 凭证的服务账号，角色只作审计身份、'
                            f'对机读调用不生效。请改凭证的作用域或数据范围。',
                })
        return attrs

    def create(self, validated_data):
        password = validated_data.pop('password', None)
        # M2M 不能进构造函数，必须存完再 set
        groups = validated_data.pop('allowed_project_groups', None)
        envs = validated_data.pop('allowed_environments', None)
        user = User(**validated_data)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save()
        if groups is not None:
            user.allowed_project_groups.set(groups)
        if envs is not None:
            user.allowed_environments.set(envs)
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


class RoleSerializer(serializers.ModelSerializer):
    """角色（模块权限矩阵 + 能力位）。

    矩阵是枚举（模块 × 四档），校验只放行已登记的模块与档位——配不出通配符、
    配不出「过宽」，写错最多是某个模块给多给少了一档。
    """

    user_count = serializers.IntegerField(source='users.count', read_only=True)

    class Meta:
        model = Role
        fields = [
            'id', 'name', 'code', 'permissions', 'can_execute', 'can_exec_prod',
            'is_builtin', 'enabled', 'remark', 'user_count', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'is_builtin', 'created_at', 'updated_at']

    def validate_code(self, value):
        value = (value or '').strip()
        if not CODE_RE.match(value):
            raise serializers.ValidationError('角色标识只能含小写字母、数字与下划线')
        if self.instance and self.instance.is_builtin and self.instance.code != value:
            raise serializers.ValidationError('内置角色的标识不可修改')
        return value

    def validate_permissions(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError('权限矩阵必须是对象 {模块: 权限档}')
        unknown_modules = sorted(set(value) - set(roles.MODULES))
        if unknown_modules:
            raise serializers.ValidationError(
                f'未知模块：{"、".join(unknown_modules)}；可选：{"、".join(roles.MODULES)}')
        bad = sorted({level for level in value.values() if level not in roles.LEVELS})
        if bad:
            raise serializers.ValidationError(
                f'未知权限档：{"、".join(bad)}；可选：{"、".join(roles.LEVELS)}')
        # 档位还要落在该模块的可选范围内（roles.MODULE_LEVELS）：审计没有写接口、
        # 制品页面上没有新增，配出来的那一档不会生效——存进去只会让人误以为
        # 自己给出去的是一个更小/更大的权限
        for module, level in value.items():
            if level not in roles.allowed_levels(module):
                raise serializers.ValidationError(
                    f'模块「{roles.MODULES[module]}」不支持「{roles.LEVEL_LABELS[level]}」档；'
                    f'可选：{"、".join(roles.LEVEL_LABELS[lv] for lv in roles.allowed_levels(module))}')
        # 缺失模块补 none，避免「没配就继承了什么」的歧义
        return {module: value.get(module, 'none') for module in roles.MODULES}

    def validate(self, attrs):
        """内置「超级管理」必须保持每个模块的最高档：否则一次手滑能把所有人锁在门外。

        比的是 `roles.top_level(module)` 而不是写死 write——audit 的最高档就是
        read（审计没有写接口），要求它 write 等于这个角色永远存不下去。
        """
        instance = self.instance
        if instance and instance.is_builtin and instance.code == 'super':
            permissions = attrs.get('permissions', instance.permissions)
            weak = sorted(m for m in roles.MODULES
                          if permissions.get(m) != roles.top_level(m))
            if weak:
                raise serializers.ValidationError(
                    f'内置角色「超级管理」必须对所有模块保持最高档（当前不足：{"、".join(weak)}），'
                    f'否则可能把所有人锁在系统之外。需要受限角色请新建一个。')
            if not (attrs.get('can_exec_prod', instance.can_exec_prod)
                    and attrs.get('can_execute', instance.can_execute)):
                raise serializers.ValidationError('内置角色「超级管理」必须保留执行与生产执行能力。')
        return attrs
