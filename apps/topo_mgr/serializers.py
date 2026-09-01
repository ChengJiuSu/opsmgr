"""拓扑画布序列化器。

节点与连线不走 ModelSerializer：它们只在「整图 PUT」里成批出现，入参校验要能逐项
指出「第 3 个节点的类型没登记」，而且要跨节点做去重与引用存在性检查。用嵌套
serializer 表达得出来，但错误信息会变成一坨按下标嵌套的字典，运维看不懂。校验逻辑
统一放 graph.py（见那里的模块文档）。
"""
import re

from rest_framework import serializers

from .models import TopoCanvas

SLUG_RE = re.compile(r'^[a-z0-9]([a-z0-9-]*[a-z0-9])?$')
# 地域与 Server.region 同口径（自由文本），但收紧字符集：画布标识会拼进
# 唯一约束，允许空格与中文会造出两条肉眼相同的画布
REGION_RE = re.compile(r'^[A-Za-z0-9_-]*$')


class TopoCanvasSerializer(serializers.ModelSerializer):
    """画布元信息。节点与连线通过 /graph/ 接口单独读写。"""

    project_group_name = serializers.CharField(source='project_group.name', read_only=True)
    project_group_code = serializers.CharField(source='project_group.code', read_only=True)
    channel_name = serializers.CharField(source='channel.name', read_only=True)
    channel_code = serializers.CharField(source='channel.code', read_only=True)
    environment_name = serializers.CharField(source='environment.name', read_only=True)
    environment_code = serializers.CharField(source='environment.code', read_only=True)
    is_prod = serializers.BooleanField(source='environment.is_prod', read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True,
                                           default='')
    node_count = serializers.IntegerField(source='nodes.count', read_only=True)
    edge_count = serializers.IntegerField(source='edges.count', read_only=True)

    class Meta:
        model = TopoCanvas
        fields = [
            'id', 'name', 'slug', 'description',
            'project_group', 'project_group_name', 'project_group_code',
            'channel', 'channel_name', 'channel_code',
            'environment', 'environment_name', 'environment_code', 'is_prod',
            'region', 'layout_mode', 'node_count', 'edge_count',
            'created_by', 'created_by_name', 'created_at', 'updated_at',
        ]
        read_only_fields = ['created_by']
        # 关闭自动生成的唯一性校验器：它会要求 region 在 PATCH 里也必须出现
        # （region 可空，前端改个名字不该被迫回传全部维度）。唯一性在 validate() 校验
        validators = []

    def validate_slug(self, value):
        value = value.strip().lower()
        if not SLUG_RE.match(value):
            raise serializers.ValidationError('画布标识只能用小写字母、数字与连字符，且不能以连字符开头结尾')
        return value

    def validate_region(self, value):
        value = value.strip()
        if not REGION_RE.match(value):
            raise serializers.ValidationError('地域只能用字母、数字、下划线与连字符（留空表示跨地域）')
        return value

    def validate(self, attrs):
        def final(name):
            if name in attrs:
                return attrs[name]
            return getattr(self.instance, name, None)

        qs = TopoCanvas.objects.filter(
            project_group=final('project_group'),
            channel=final('channel'),
            environment=final('environment'),
            region=final('region') or '',
            slug=final('slug'),
        )
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError(
                '该项目组/渠道/环境/地域下已存在同标识的画布，请换一个画布标识')
        return attrs
