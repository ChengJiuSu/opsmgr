"""内置导航页占位数据：与改造前 nav.html 内的示例清单一致。

幂等（全部 get_or_create），可回滚（reverse 按内置编码精确删除，不碰后加的数据）。
真实清单上线后直接在「配置管理 → 内部导航页」里改，这份种子不再参与。
"""

from django.db import migrations
from django.utils import timezone

SETTING = {
    'sidebar_width': 272,
    'max_cols': 4,
    'recent_max': 8,
    'masthead_open_by_default': True,
    'brand_mark': 'N',
    'brand_name': '内部系统导航台',
    'brand_sub': 'Internal Systems Index',
    'masthead_eyebrow': 'Corp Intranet / 2026',
    'masthead_title': '公司内部系统\n统一入口',
    'masthead_intro': (
        '这里收录公司在用的全部内部系统入口，按运维、平台、公共服务、文档四类归档。'
        '账号统一走单点登录；若某个系统提示无权限，请按卡片所属小节向对应负责团队申请。'
        '清单每周同步一次。'
    ),
    'masthead_systems_label': '系统总数',
    'masthead_groups_label': '一级类别',
    'footer': '维护方：**基础架构部 · 运维平台组**\n新增或下线系统请提工单 **#IT-NAV**',
}

# (code, title, tag)
BLOCKS = [
    ("monitor", "监控告警", "运维工具"),
    ("cicd", "发布与流水线", "运维工具"),
    ("infra", "主机与资源", "运维工具"),
    ("apps", "应用平台", "平台工具"),
    ("data", "数据与报表", "平台工具"),
    ("office", "办公协同", "公共服务"),
    ("hr", "人事财务", "公共服务"),
    ("techdoc", "技术文档", "文档与知识库"),
    ("process", "流程规范", "文档与知识库"),
]

# (block_code, name, abbr, description, domain, url)
SYSTEMS = [
    ("monitor", "统一监控中心", "MON", "主机、容器与业务指标的统一大盘，含告警规则与静默配置。", "monitor.corp.internal", "https://monitor.corp.internal"),
    ("monitor", "日志检索平台", "LOG", "全量应用日志采集与检索，支持按 traceId 串联上下游。", "log.corp.internal", "https://log.corp.internal"),
    ("monitor", "链路追踪", "APM", "分布式调用链与慢接口分析，定位跨服务性能瓶颈。", "apm.corp.internal", "https://apm.corp.internal"),
    ("monitor", "值班与告警", "DUT", "排班表、告警升级策略与故障处理记录。", "duty.corp.internal", "https://duty.corp.internal"),
    ("cicd", "持续集成", "CI", "代码提交后的构建、单测与镜像打包流水线。", "ci.corp.internal", "https://ci.corp.internal"),
    ("cicd", "制品仓库", "ART", "容器镜像、Maven 与 npm 私有仓库，含保留策略。", "artifact.corp.internal", "https://artifact.corp.internal"),
    ("cicd", "发布中心", "REL", "灰度发布、回滚与发布窗口审批，串联变更单。", "release.corp.internal", "https://release.corp.internal"),
    ("infra", "资产配置库", "CMDB", "主机、集群、域名与服务归属关系的唯一事实来源。", "cmdb.corp.internal", "https://cmdb.corp.internal"),
    ("infra", "堡垒机", "BAS", "生产环境统一登录入口，全程录屏审计。", "bastion.corp.internal", "https://bastion.corp.internal"),
    ("infra", "云资源控制台", "CLD", "多云账号下的实例、网络与账单聚合视图。", "cloud.corp.internal", "https://cloud.corp.internal"),
    ("infra", "域名与证书", "DNS", "内外网解析记录、证书申请与到期提醒。", "dns.corp.internal", "https://dns.corp.internal"),
    ("apps", "容器平台", "K8S", "应用编排与扩缩容，命名空间按业务线划分。", "paas.corp.internal", "https://paas.corp.internal"),
    ("apps", "配置中心", "CFG", "环境配置与开关灰度下发，改动留审计记录。", "config.corp.internal", "https://config.corp.internal"),
    ("apps", "任务调度", "JOB", "定时任务编排与执行历史，支持失败重试与告警。", "job.corp.internal", "https://job.corp.internal"),
    ("data", "数据看板", "BI", "经营与业务指标报表，按角色分配查看范围。", "bi.corp.internal", "https://bi.corp.internal"),
    ("data", "指标平台", "MET", "指标口径定义与血缘，避免同名不同义。", "metrics.corp.internal", "https://metrics.corp.internal"),
    ("data", "数据开发", "ETL", "离线与实时任务开发、调度与数据质量校验。", "etl.corp.internal", "https://etl.corp.internal"),
    ("data", "实验平台", "EXP", "A/B 分流配置与效果分析，含显著性检验。", "ab.corp.internal", "https://ab.corp.internal"),
    ("office", "企业邮箱", "MAIL", "公司邮箱 Web 端，支持邮件组与共享日历。", "mail.corp.internal", "https://mail.corp.internal"),
    ("office", "内部即时通讯", "IM", "团队沟通与机器人通知的网页端入口。", "im.corp.internal", "https://im.corp.internal"),
    ("office", "会议室预订", "ROOM", "各办公区会议室与工位预订，可绑定日历。", "room.corp.internal", "https://room.corp.internal"),
    ("office", "文件云盘", "DRV", "部门共享目录与文档外链管理，含权限回收。", "drive.corp.internal", "https://drive.corp.internal"),
    ("hr", "人事系统", "HR", "考勤、请假、绩效与个人信息维护。", "hr.corp.internal", "https://hr.corp.internal"),
    ("hr", "费用报销", "FIN", "差旅与日常报销提单，含发票查验与进度跟踪。", "fin.corp.internal", "https://fin.corp.internal"),
    ("hr", "采购与合同", "BUY", "采购申请、供应商信息与合同用印流程。", "buy.corp.internal", "https://buy.corp.internal"),
    ("techdoc", "研发知识库", "WIKI", "各团队技术文档、故障复盘与上手指引。", "wiki.corp.internal", "https://wiki.corp.internal"),
    ("techdoc", "接口文档", "API", "内部服务 OpenAPI 文档与在线调试。", "api-doc.corp.internal", "https://api-doc.corp.internal"),
    ("techdoc", "架构决策记录", "ADR", "重大技术选型的背景、取舍与结论归档。", "adr.corp.internal", "https://adr.corp.internal"),
    ("process", "工单系统", "TIC", "IT 支持、权限申请与资源开通的统一提单入口。", "ticket.corp.internal", "https://ticket.corp.internal"),
    ("process", "变更与审批", "CHG", "生产变更登记、风险评估与审批流转。", "change.corp.internal", "https://change.corp.internal"),
    ("process", "安全合规", "SEC", "安全基线、漏洞处置与合规制度查阅。", "sec.corp.internal", "https://sec.corp.internal"),
]

# (group_name, en_name, [(label, block_code), ...])
NAV = [
    ("运维工具", "Operations", [("监控告警", "monitor"), ("发布与流水线", "cicd"), ("主机与资源", "infra")]),
    ("平台工具", "Platform", [("应用平台", "apps"), ("数据与报表", "data")]),
    ("公共服务", "Workplace", [("办公协同", "office"), ("人事财务", "hr")]),
    ("文档与知识库", "Knowledge", [("技术文档", "techdoc"), ("流程规范", "process")]),
]

# (code, title, [system_name, ...], start, end, body)
NOTICES = [
    ("2026-08-CHG-114", "核心网络设备固件升级", ["统一监控中心", "堡垒机", "制品仓库"], "2026-08-29 22:00", "2026-09-01 06:00", "升级期间上述系统会出现 2～5 分钟的间歇性不可用，登录会话可能被断开，重新登录即可。\n构建流水线请避开该窗口触发；已排队任务会在升级完成后自动继续。\n如遇异常请在工单系统提单并标注「网络升级」。"),
    ("2026-07-CHG-098", "报销系统版本切换", ["费用报销"], "2026-07-12 20:00", "2026-07-13 02:00", "历史公告，仅作示例，已过生效窗口。"),
]


def _dt(text):
    """'2026-08-29 22:00' → 当前时区的 aware datetime。"""
    from datetime import datetime
    naive = datetime.strptime(text, '%Y-%m-%d %H:%M')
    return timezone.make_aware(naive, timezone.get_current_timezone())


def forwards(apps, schema_editor):
    Setting = apps.get_model('portal_mgr', 'PortalSetting')
    Block = apps.get_model('portal_mgr', 'PortalBlock')
    System = apps.get_model('portal_mgr', 'PortalSystem')
    NavGroup = apps.get_model('portal_mgr', 'PortalNavGroup')
    NavItem = apps.get_model('portal_mgr', 'PortalNavItem')
    Notice = apps.get_model('portal_mgr', 'PortalNotice')

    Setting.objects.get_or_create(pk=1, defaults=SETTING)

    block_by_code = {}
    for i, (code, title, tag) in enumerate(BLOCKS):
        obj, _ = Block.objects.get_or_create(
            code=code, defaults={'title': title, 'tag': tag, 'sort_order': (i + 1) * 10})
        block_by_code[code] = obj

    sys_by_name = {}
    for i, (block_code, name, abbr, desc, domain, url) in enumerate(SYSTEMS):
        obj, _ = System.objects.get_or_create(
            domain=domain,
            defaults={
                'block': block_by_code[block_code], 'name': name, 'abbr': abbr,
                'description': desc, 'url': url, 'sort_order': (i + 1) * 10,
            })
        sys_by_name[name] = obj

    for gi, (name, en, items) in enumerate(NAV):
        group, _ = NavGroup.objects.get_or_create(
            name=name, defaults={'en_name': en, 'sort_order': (gi + 1) * 10})
        for ii, (label, block_code) in enumerate(items):
            NavItem.objects.get_or_create(
                group=group, block=block_by_code[block_code],
                defaults={'label': label, 'sort_order': (ii + 1) * 10})

    for code, title, system_names, start, end, body in NOTICES:
        notice, created = Notice.objects.get_or_create(
            code=code,
            defaults={'title': title, 'start_at': _dt(start), 'end_at': _dt(end), 'body': body})
        if created:
            notice.systems.set([sys_by_name[n] for n in system_names if n in sys_by_name])


def backwards(apps, schema_editor):
    """按内置编码精确回滚，后来手工添加的配置不受影响。"""
    apps.get_model('portal_mgr', 'PortalNotice').objects.filter(
        code__in=[n[0] for n in NOTICES]).delete()
    apps.get_model('portal_mgr', 'PortalNavGroup').objects.filter(
        name__in=[g[0] for g in NAV]).delete()      # 导航项随分组级联删除
    apps.get_model('portal_mgr', 'PortalSystem').objects.filter(
        domain__in=[s[4] for s in SYSTEMS]).delete()
    apps.get_model('portal_mgr', 'PortalBlock').objects.filter(
        code__in=[b[0] for b in BLOCKS]).delete()
    apps.get_model('portal_mgr', 'PortalSetting').objects.filter(pk=1).delete()


class Migration(migrations.Migration):

    dependencies = [('portal_mgr', '0001_initial')]

    operations = [migrations.RunPython(forwards, backwards)]
