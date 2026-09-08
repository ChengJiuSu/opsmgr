from rest_framework import serializers

from apps.asset_mgr.models import Channel, Environment, ProjectGroup, ServiceType

from .models import FILENAME_RE, VERSION_RE, Artifact


class ArtifactSerializer(serializers.ModelSerializer):
    """制品列表/详情展示（含维度名称与带签名下载地址）。"""

    project_group_name = serializers.CharField(source='project_group.name', read_only=True)
    channel_name = serializers.CharField(source='channel.name', read_only=True)
    environment_name = serializers.CharField(source='environment.name', read_only=True)
    service_type_name = serializers.CharField(source='service_type.name', read_only=True, default='')
    download_url = serializers.SerializerMethodField()

    class Meta:
        model = Artifact
        fields = [
            'id', 'project_group', 'project_group_name', 'channel', 'channel_name',
            'environment', 'environment_name', 'service_type', 'service_type_name',
            'version', 'original_name',
            'md5', 'size', 'jenkins_job', 'build_number', 'source', 'remark', 'reported_at',
            'download_url',
        ]

    def get_download_url(self, obj):
        return obj.download_url()


def _resolve_dict(model, raw, label):
    """维度入参解析：纯数字按 id、否则按 code（与服务器/变量接口口径一致）。"""
    raw = (raw or '').strip()
    if not raw:
        raise serializers.ValidationError(f'{label}不能为空')
    qs = model.objects.filter(pk=raw) if raw.isdigit() else model.objects.filter(code=raw)
    obj = qs.first()
    if obj is None:
        raise serializers.ValidationError(f'{label}不存在：{raw}')
    if not obj.enabled:
        raise serializers.ValidationError(f'{label}已停用：{raw}')
    return obj


class ArtifactReportSerializer(serializers.Serializer):
    """CI 上报入参：multipart（file + 维度/服务类型 code/id + 版本 + 可选 md5 等）。"""

    file = serializers.FileField()
    project_group = serializers.CharField()
    channel = serializers.CharField()
    environment = serializers.CharField()
    # 服务类型必填：同一 项目组/渠道/环境 下并存多个服务，缺了它 CD 取包只能按
    # 「最新」猜，会把别的服务的包发出去
    service_type = serializers.CharField()
    version = serializers.CharField(max_length=100)
    md5 = serializers.RegexField(r'^[0-9a-fA-F]{32}$', required=False, allow_blank=True,
                                 error_messages={'invalid': 'md5 必须为 32 位十六进制字符串'})
    jenkins_job = serializers.CharField(max_length=150, required=False, allow_blank=True, default='')
    build_number = serializers.IntegerField(required=False, allow_null=True, default=None, min_value=0)
    remark = serializers.CharField(max_length=255, required=False, allow_blank=True, default='')

    def validate_version(self, value):
        value = value.strip()
        if not VERSION_RE.match(value):
            raise serializers.ValidationError('版本号仅允许字母、数字、. _ -，且不超过 100 字符')
        return value

    def validate_file(self, value):
        if not FILENAME_RE.match(value.name or ''):
            raise serializers.ValidationError('文件名仅允许字母、数字、. _ -，且不超过 255 字符')
        return value

    def validate(self, attrs):
        attrs['project_group'] = _resolve_dict(ProjectGroup, attrs['project_group'], '项目组')
        attrs['channel'] = _resolve_dict(Channel, attrs['channel'], '渠道')
        attrs['environment'] = _resolve_dict(Environment, attrs['environment'], '环境')
        attrs['service_type'] = _resolve_dict(ServiceType, attrs['service_type'], '服务类型')
        return attrs
