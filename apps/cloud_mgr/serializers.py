"""云控管理序列化器。"""
import re

from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .models import CertDeployment, Certificate, CloudAccount, DnsRecord, Domain

# 域名整体校验（简化 RFC 1035：小写字母/数字/连字符，点分，末级 ≥2 位字母）
DOMAIN_RE = re.compile(r'^(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))+$')
# 主机记录：@ / * / *.xx / 常规标签点分
RR_RE = re.compile(r'^(@|\*|(\*\.)?(?!-)[a-zA-Z0-9_-]{1,63}(?<!-)(\.(?!-)[a-zA-Z0-9_-]{1,63}(?<!-))*)$')


class CloudAccountSerializer(serializers.ModelSerializer):
    """云账号序列化器。secret_key 只写不回显，has_secret_key 标志是否已设置。"""

    provider_name = serializers.CharField(source='provider.name', read_only=True)
    provider_code = serializers.CharField(source='provider.code', read_only=True)
    secret_key = serializers.CharField(write_only=True, required=False, allow_blank=True)
    has_secret_key = serializers.SerializerMethodField()
    domain_count = serializers.IntegerField(source='domains.count', read_only=True)

    class Meta:
        model = CloudAccount
        fields = [
            'id', 'name', 'provider', 'provider_name', 'provider_code',
            'access_key', 'secret_key', 'has_secret_key', 'domain_count',
            'enabled', 'remark', 'created_at', 'updated_at',
        ]

    @extend_schema_field(OpenApiTypes.BOOL)
    def get_has_secret_key(self, obj):
        return bool(obj.secret_key)

    def validate(self, attrs):
        # 新建必须携带 secret_key；编辑留空表示不修改
        if self.instance is None and not attrs.get('secret_key'):
            raise serializers.ValidationError('新建云账号必须填写 AccessKey Secret')
        if self.instance is not None and not attrs.get('secret_key'):
            attrs.pop('secret_key', None)
        return attrs


class DomainSerializer(serializers.ModelSerializer):
    """主域名序列化器。"""

    cloud_account_name = serializers.CharField(source='cloud_account.name', read_only=True)
    provider_name = serializers.CharField(source='cloud_account.provider.name', read_only=True)
    provider_code = serializers.CharField(source='cloud_account.provider.code', read_only=True)
    record_count = serializers.IntegerField(source='records.count', read_only=True)

    class Meta:
        model = Domain
        fields = [
            'id', 'name', 'cloud_account', 'cloud_account_name',
            'provider_name', 'provider_code', 'status', 'record_count',
            'remark', 'last_synced_at', 'created_at', 'updated_at',
        ]
        read_only_fields = ['status', 'last_synced_at']

    def validate_name(self, value):
        value = value.strip().lower().rstrip('.')
        if not DOMAIN_RE.match(value):
            raise serializers.ValidationError('域名格式不正确')
        return value


class DnsRecordSerializer(serializers.ModelSerializer):
    """解析记录序列化器。

    - target_type=server 时 server 必填，value 自动取该主机 IP（公网优先，NAT 主机回退私网）；
    - 其余 target_type 手工填 value；
    - _acme-challenge 前缀记录保留给证书验证流程，禁止手工创建。
    """

    domain_name = serializers.CharField(source='domain.name', read_only=True)
    server_name = serializers.CharField(source='server.name', read_only=True, default=None)
    fqdn = serializers.CharField(read_only=True)
    # 绑定服务器时 value 由后端按主机 IP 生成，字段级不强制
    value = serializers.CharField(required=False, allow_blank=True, max_length=512)

    class Meta:
        model = DnsRecord
        fields = [
            'id', 'domain', 'domain_name', 'rr', 'fqdn', 'type', 'value', 'ttl',
            'line', 'target_type', 'server', 'server_name',
            'remote_record_id', 'sync_status', 'sync_error',
            'enabled', 'remark', 'created_at', 'updated_at',
        ]
        read_only_fields = ['remote_record_id', 'sync_status', 'sync_error']
        # 关闭自动生成的 UniqueTogetherValidator（会强制 value 必填，
        # 与"绑定服务器时 value 由后端填充"冲突），唯一性在 validate() 手动校验
        validators = []

    def validate_rr(self, value):
        value = value.strip()
        if not RR_RE.match(value):
            raise serializers.ValidationError('主机记录格式不正确（支持 @、*、*.xx、字母数字连字符）')
        if value == '_acme-challenge' or value.startswith('_acme-challenge.'):
            raise serializers.ValidationError('_acme-challenge 记录由证书验证流程自动管理，禁止手工维护')
        return value

    def validate_ttl(self, value):
        if not 1 <= value <= 86400:
            raise serializers.ValidationError('TTL 须在 1~86400 秒之间')
        return value

    def validate(self, attrs):
        target_type = attrs.get('target_type', getattr(self.instance, 'target_type', 'none'))
        server = attrs.get('server', getattr(self.instance, 'server', None))
        rtype = attrs.get('type', getattr(self.instance, 'type', 'A'))
        if target_type == 'server':
            if server is None:
                raise serializers.ValidationError('绑定类型为服务器时必须选择服务器')
            if rtype not in ('A', 'AAAA'):
                raise serializers.ValidationError('绑定服务器仅支持 A/AAAA 记录')
            # 与平台连接口径一致：公网优先，NAT 主机回退私网（内网解析场景）
            attrs['value'] = server.conn_ip
        else:
            attrs['server'] = None
            if not (attrs.get('value') or getattr(self.instance, 'value', '')):
                raise serializers.ValidationError('记录值不能为空')

        # 唯一性手动校验（替代自动 UniqueTogetherValidator，需在 value 填充后执行）
        def _final(name):
            return attrs[name] if name in attrs else getattr(self.instance, name, None)

        qs = DnsRecord.objects.filter(
            domain=_final('domain'), rr=_final('rr'), type=_final('type'),
            value=_final('value'), line=_final('line') or 'default',
        )
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError('同域名下已存在相同的解析记录（主机记录+类型+值+线路重复）')
        return attrs


# 证书域名项：支持通配符（*.example.com）
CERT_DOMAIN_RE = re.compile(r'^(\*\.)?(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))+$')


class CertificateSerializer(serializers.ModelSerializer):
    """证书序列化器。私钥不回显（has_private_key 标志）；PEM 仅 download 接口返回。

    domains 为 SAN 列表，首项为主域名（acme.sh 证书目录按首项命名，签发后不可改）。
    """

    cloud_account_name = serializers.CharField(source='cloud_account.name', read_only=True)
    provider_code = serializers.CharField(source='cloud_account.provider.code', read_only=True)
    has_private_key = serializers.SerializerMethodField()
    days_left = serializers.SerializerMethodField()
    deployment_count = serializers.IntegerField(source='deployments.count', read_only=True)

    class Meta:
        model = Certificate
        fields = [
            'id', 'name', 'domains', 'cloud_account', 'cloud_account_name', 'provider_code',
            'status', 'has_private_key', 'issued_at', 'expires_at', 'days_left',
            'auto_renew', 'deployment_count', 'last_error', 'remark', 'created_at', 'updated_at',
        ]
        read_only_fields = ['status', 'issued_at', 'expires_at', 'last_error']

    @extend_schema_field(OpenApiTypes.BOOL)
    def get_has_private_key(self, obj):
        return bool(obj.private_key_pem)

    @extend_schema_field(OpenApiTypes.INT)
    def get_days_left(self, obj):
        if not obj.expires_at:
            return None
        return (obj.expires_at - timezone.now()).days

    def validate_domains(self, value):
        if not isinstance(value, list) or not value:
            raise serializers.ValidationError('域名列表不能为空')
        cleaned = []
        for item in value:
            d = str(item).strip().lower().rstrip('.')
            if not CERT_DOMAIN_RE.match(d):
                raise serializers.ValidationError(f'域名格式不正确：{item}')
            if d in cleaned:
                raise serializers.ValidationError(f'域名重复：{d}')
            cleaned.append(d)
        if cleaned[0].startswith('*.'):
            raise serializers.ValidationError('首个域名为证书主域名，不能是通配符（可将通配符放后面）')
        return cleaned

    def validate(self, attrs):
        # 签发后锁定域名与验证账号（acme home 目录按主域名组织，改列表须新建证书）
        if self.instance is not None and self.instance.status not in ('pending', 'failed'):
            for field in ('domains', 'cloud_account'):
                if field in attrs and attrs[field] != getattr(self.instance, field):
                    raise serializers.ValidationError('已签发证书不能修改域名列表 / 验证账号，请新建证书')
        return attrs


class CertDeploymentSerializer(serializers.ModelSerializer):
    """证书分发目标序列化器。

    - target_type=server 时 server 必填；其他类型为预留（deploy.py 桩），
      仅允许登记不允许执行（视图层拦截并给出中文提示）；
    - 部署目录必须为绝对路径，禁止根目录等危险路径。
    """

    certificate_name = serializers.CharField(source='certificate.name', read_only=True)
    server_name = serializers.CharField(source='server.name', read_only=True, default=None)
    server_ip = serializers.CharField(source='server.conn_ip', read_only=True, default=None)
    credential_name = serializers.CharField(source='credential.name', read_only=True, default=None)
    target_type_display = serializers.CharField(source='get_target_type_display', read_only=True)

    class Meta:
        model = CertDeployment
        fields = [
            'id', 'certificate', 'certificate_name', 'target_type', 'target_type_display',
            'server', 'server_name', 'server_ip', 'credential', 'credential_name',
            'deploy_path', 'reload_cmd', 'auto_deploy',
            'status', 'last_deployed_at', 'last_error', 'last_output',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['status', 'last_deployed_at', 'last_error', 'last_output']

    def validate_deploy_path(self, value):
        value = (value or '').strip().rstrip('/')
        if not value.startswith('/'):
            raise serializers.ValidationError('部署目录须为绝对路径，如 /etc/nginx/ssl')
        if value in ('', '/bin', '/sbin', '/usr', '/etc', '/boot', '/dev', '/proc', '/sys'):
            raise serializers.ValidationError(f'禁止部署到系统关键目录：{value or "/"}')
        return value

    def validate(self, attrs):
        target_type = attrs.get('target_type', getattr(self.instance, 'target_type', 'server'))
        server = attrs.get('server', getattr(self.instance, 'server', None))
        if target_type == 'server':
            if server is None:
                raise serializers.ValidationError('目标类型为服务器时必须选择服务器')
        else:
            attrs['server'] = None
        return attrs
