"""种子数据 + 存量数据回填。

1. 内置常用环境 / 云厂商 / 渠道字典（get_or_create，可重复执行）；
2. 将服务器上旧的 env / department 文本值转换为环境 / 项目组字典并回填外键，
   保证已有生产数据平滑迁移（本迁移之后 0005 才会删除旧列）。
"""
import re

from django.db import migrations

# 旧 env 编码 → 环境名称映射，未命中时直接用原值作名称
ENV_NAME_MAP = {
    'dev': '开发',
    'test': '测试',
    'uat': '预发',
    'staging': '预发',
    'prod': '生产',
    'production': '生产',
}

# 内置环境：code, 名称, 是否生产, 排序
ENV_SEEDS = [
    ('dev', '开发', False, 1),
    ('test', '测试', False, 2),
    ('prod', '生产', True, 3),
]

# 内置云厂商：code, 名称, 排序
PROVIDER_SEEDS = [
    ('aliyun', '阿里云', 1),
    ('tencent', '腾讯云', 2),
    ('huawei', '华为云', 3),
    ('aws', 'AWS', 4),
    ('gcp', 'GCP', 5),
    ('idc', '自建机房', 6),
]

# 内置渠道：code, 名称, 平台, 排序
CHANNEL_SEEDS = [
    ('qixia', '奇侠', 'all', 1),
    ('bilibili', 'B站', 'all', 2),
]

SLUG_RE = re.compile(r'^[-a-zA-Z0-9_]+$')


def seedAndBackfill(apps, schema_editor):
    Environment = apps.get_model('asset_mgr', 'Environment')
    CloudProvider = apps.get_model('asset_mgr', 'CloudProvider')
    Channel = apps.get_model('asset_mgr', 'Channel')
    ProjectGroup = apps.get_model('asset_mgr', 'ProjectGroup')
    Server = apps.get_model('asset_mgr', 'Server')

    # ---------- 种子数据 ----------
    for code, name, isProd, sort in ENV_SEEDS:
        Environment.objects.get_or_create(
            code=code, defaults={'name': name, 'is_prod': isProd, 'sort_order': sort},
        )
    for code, name, sort in PROVIDER_SEEDS:
        CloudProvider.objects.get_or_create(
            code=code, defaults={'name': name, 'sort_order': sort},
        )
    for code, name, platform, sort in CHANNEL_SEEDS:
        Channel.objects.get_or_create(
            code=code, defaults={'name': name, 'platform': platform, 'sort_order': sort},
        )

    # ---------- 回填：env 文本 → 环境外键 ----------
    envValues = (
        Server.objects.exclude(env='').values_list('env', flat=True).distinct()
    )
    for index, value in enumerate(sorted(envValues), start=1):
        # 旧值本身可作合法编码则直接用，否则自动生成编码、原值作名称
        code = value if SLUG_RE.match(value) else f'env{index}'
        envObj, _ = Environment.objects.get_or_create(
            code=code,
            defaults={
                'name': ENV_NAME_MAP.get(value, value),
                'is_prod': value in ('prod', 'production'),
            },
        )
        Server.objects.filter(env=value).update(environment=envObj)

    # ---------- 回填：department 文本 → 项目组外键 ----------
    deptValues = (
        Server.objects.exclude(department='').values_list('department', flat=True).distinct()
    )
    for index, value in enumerate(sorted(deptValues), start=1):
        # 部门名多为中文，无法 slug 化，统一生成 group{n} 编码，名称保留原值
        code = value if SLUG_RE.match(value) else f'group{index}'
        groupObj, _ = ProjectGroup.objects.get_or_create(
            code=code, defaults={'name': value},
        )
        Server.objects.filter(department=value).update(project_group=groupObj)


class Migration(migrations.Migration):

    dependencies = [
        ('asset_mgr', '0003_channel_cloudprovider_environment_projectgroup_and_more'),
    ]

    operations = [
        # 回滚时仅解除外键引用，不删除字典数据
        migrations.RunPython(seedAndBackfill, migrations.RunPython.noop),
    ]
