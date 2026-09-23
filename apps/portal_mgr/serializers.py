"""导航页配置序列化器。

写侧（管理页 CRUD）与读侧（导航页聚合取数）分开：管理页要的是可编辑的原始
字段，导航页要的是能直接喂给渲染函数的形状，混在一个序列化器里会互相迁就。
"""

from django.utils import timezone
from rest_framework import serializers

from .models import (
    PortalBlock,
    PortalNavGroup,
    PortalNavItem,
    PortalNotice,
    PortalSetting,
    PortalSystem,
)


# ── 管理页：CRUD ──────────────────────────────────────────

class PortalSettingSerializer(serializers.ModelSerializer):
    class Meta:
        model = PortalSetting
        exclude = ('id',)


class PortalBlockSerializer(serializers.ModelSerializer):
    system_count = serializers.IntegerField(source='systems.count', read_only=True)

    class Meta:
        model = PortalBlock
        fields = '__all__'


class PortalSystemSerializer(serializers.ModelSerializer):
    block_title = serializers.CharField(source='block.title', read_only=True)
    block_code = serializers.CharField(source='block.code', read_only=True)

    class Meta:
        model = PortalSystem
        fields = '__all__'


class PortalNavItemSerializer(serializers.ModelSerializer):
    block_title = serializers.CharField(source='block.title', read_only=True)
    group_name = serializers.CharField(source='group.name', read_only=True)

    class Meta:
        model = PortalNavItem
        fields = '__all__'

    def validate(self, attrs):
        # 模型 clean() 不会被 DRF 自动调用，二选一规则在这里再判一次
        merged = {**{f: getattr(self.instance, f, None) for f in ('block', 'href', 'label')}, **attrs}
        if bool(merged.get('block')) == bool(merged.get('href')):
            raise serializers.ValidationError('导航项需要且只能二选一：指向区块，或填写外链地址')
        if merged.get('href') and not merged.get('label'):
            raise serializers.ValidationError('外链导航项必须填写显示名称')
        return attrs


class PortalNavGroupSerializer(serializers.ModelSerializer):
    items = PortalNavItemSerializer(many=True, read_only=True)

    class Meta:
        model = PortalNavGroup
        fields = '__all__'


class PortalNoticeSerializer(serializers.ModelSerializer):
    system_names = serializers.SerializerMethodField()
    is_active = serializers.SerializerMethodField()

    class Meta:
        model = PortalNotice
        fields = '__all__'

    def get_system_names(self, obj) -> list[str]:
        return [s.name for s in obj.systems.all()]

    def get_is_active(self, obj) -> bool:
        now = timezone.now()
        return bool(obj.enabled and obj.start_at <= now <= obj.end_at)

    def validate(self, attrs):
        start = attrs.get('start_at', getattr(self.instance, 'start_at', None))
        end = attrs.get('end_at', getattr(self.instance, 'end_at', None))
        if start and end and start >= end:
            raise serializers.ValidationError('结束时间必须晚于开始时间')
        return attrs


# ── 导航页：聚合取数 ───────────────────────────────────────

def _lines(text):
    """多行文本拆成数组，去掉空行与行尾空白。"""
    return [ln.rstrip() for ln in (text or '').splitlines() if ln.strip()]


def _footer_line(line):
    """把 `前缀**加粗**后缀` 拆成分段数组，奇数段由页面加粗渲染。

    行尾若正好以加粗收尾，split 会多出一个空串，去掉它——留着虽不显示，但会让
    接口返回值看起来像有个空段。
    """
    segs = line.split('**')
    while segs and segs[-1] == '':
        segs.pop()
    return segs


def _fmt(dt):
    """公告时间转成导航页解析的 'YYYY-MM-DD HH:MM'（本地时区）。"""
    return timezone.localtime(dt).strftime('%Y-%m-%d %H:%M')


def build_config():
    """组装导航页启动时需要的整份配置。

    只吐启用中的数据；导航项指向的区块若被停用，该导航项一并略过——否则侧栏
    会出现一个点了滚不过去的条目。
    """
    setting = PortalSetting.load()
    blocks = list(PortalBlock.objects.filter(enabled=True))
    live_codes = {b.code for b in blocks}

    groups = []
    for g in PortalNavGroup.objects.filter(enabled=True).prefetch_related('items__block'):
        items = []
        for it in g.items.filter(enabled=True):
            if it.block:
                if it.block.code not in live_codes:
                    continue
                items.append({'label': it.label or it.block.title, 'block': it.block.code})
            else:
                items.append({'label': it.label, 'href': it.href})
        if items:
            groups.append({'group': g.name, 'en': g.en_name, 'items': items})

    systems = [
        {
            'block': s.block.code,
            'name': s.name,
            'abbr': s.abbr,
            'desc': s.description,
            'domain': s.domain,
            'url': s.url,
        }
        for s in PortalSystem.objects.filter(enabled=True, block__enabled=True)
                                     .select_related('block')
    ]

    notices = [
        {
            'id': n.code,
            'title': n.title,
            'systems': [s.name for s in n.systems.all()],
            'start': _fmt(n.start_at),
            'end': _fmt(n.end_at),
            'body': n.body,
        }
        for n in PortalNotice.objects.filter(enabled=True).prefetch_related('systems')
    ]

    return {
        'settings': {
            'sidebarWidth': setting.sidebar_width,
            'maxCols': setting.max_cols,
            'recentMax': setting.recent_max,
            'mastheadOpenByDefault': setting.masthead_open_by_default,
            'brand': {
                'mark': setting.brand_mark,
                'name': setting.brand_name,
                'sub': setting.brand_sub,
            },
            'footer': [_footer_line(ln) for ln in _lines(setting.footer)],
            'masthead': {
                'eyebrow': setting.masthead_eyebrow,
                'title': _lines(setting.masthead_title),
                'intro': setting.masthead_intro,
                'systemsLabel': setting.masthead_systems_label,
                'groupsLabel': setting.masthead_groups_label,
            },
        },
        'nav': groups,
        'blocks': [{'id': b.code, 'title': b.title, 'tag': b.tag} for b in blocks],
        'systems': systems,
        'notices': notices,
    }
