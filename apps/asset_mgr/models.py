import re

from django.db import models


class DictBase(models.Model):
    """CMDB 字典表公共基类：名称 + 唯一编码 + 启用开关 + 排序。"""

    name = models.CharField('名称', max_length=100)
    code = models.SlugField('编码', max_length=50, unique=True)
    enabled = models.BooleanField('是否启用', default=True)
    sort_order = models.PositiveIntegerField('排序', default=0)
    remark = models.TextField('备注', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

    def __str__(self):
        return self.name


class ProjectGroup(DictBase):
    """项目组：游戏项目/团队维度，替代原服务器上的自由文本"部门"。"""

    owner = models.CharField('负责人', max_length=50, blank=True)
    contact = models.CharField('联系方式', max_length=100, blank=True)

    class Meta:
        verbose_name = '项目组'
        verbose_name_plural = '项目组'
        db_table = 'asset_project_group'
        ordering = ['sort_order', 'id']


class Channel(DictBase):
    """渠道：游戏发行/联运渠道（官服、华为、TapTap、AppStore 等）。"""

    PLATFORM_CHOICES = [
        ('all', '全平台'),
        ('android', '安卓'),
        ('ios', 'iOS'),
    ]

    platform = models.CharField('平台', max_length=10, choices=PLATFORM_CHOICES, default='all')

    class Meta:
        verbose_name = '渠道'
        verbose_name_plural = '渠道'
        db_table = 'asset_channel'
        ordering = ['sort_order', 'id']


class Environment(DictBase):
    """环境：开发/测试/生产等，is_prod 用于生产环境高危操作识别。"""

    is_prod = models.BooleanField('是否生产环境', default=False)

    class Meta:
        verbose_name = '环境'
        verbose_name_plural = '环境'
        db_table = 'asset_environment'
        ordering = ['sort_order', 'id']


class CloudProvider(DictBase):
    """云厂商：阿里云/腾讯云/AWS 等，自建机房也作为一种"厂商"维护。"""

    class Meta:
        verbose_name = '云厂商'
        verbose_name_plural = '云厂商'
        db_table = 'asset_cloud_provider'
        ordering = ['sort_order', 'id']


class ServiceType(DictBase):
    """服务类型：主机可承载的服务（游戏服/网关/登录/DB/Redis/Nginx 等）。

    与主机的关联关系（已规划/已部署/已下线）见 ServerService。
    """

    class Meta:
        verbose_name = '服务类型'
        verbose_name_plural = '服务类型'
        db_table = 'asset_service_type'
        ordering = ['sort_order', 'id']


class Server(models.Model):
    STATUS_CHOICES = [
        ('running', '运行中'),
        ('stopped', '已停止'),
        ('maintenance', '维护中'),
    ]

    name = models.CharField('服务器名称', max_length=100)
    ip = models.GenericIPAddressField('IP 地址')
    os = models.CharField('操作系统', max_length=50, blank=True)
    cpu = models.PositiveSmallIntegerField('CPU 核数', default=0)
    memory = models.PositiveIntegerField('内存 MB', default=0)
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='running')
    # CMDB 字典外键：PROTECT 防止误删仍被引用的基础数据
    # （原 env / department 文本字段已由 environment / project_group 外键替代，见 0004/0005 迁移）
    project_group = models.ForeignKey(
        ProjectGroup, verbose_name='项目组', on_delete=models.PROTECT,
        null=True, blank=True, related_name='servers',
    )
    channel = models.ForeignKey(
        Channel, verbose_name='渠道', on_delete=models.PROTECT,
        null=True, blank=True, related_name='servers',
    )
    environment = models.ForeignKey(
        Environment, verbose_name='环境', on_delete=models.PROTECT,
        null=True, blank=True, related_name='servers',
    )
    cloud_provider = models.ForeignKey(
        CloudProvider, verbose_name='云厂商', on_delete=models.PROTECT,
        null=True, blank=True, related_name='servers',
    )
    region = models.CharField('地域/机房', max_length=50, blank=True)
    ssh_port = models.PositiveIntegerField('SSH 端口', default=22)
    ssh_user = models.CharField('SSH 用户', max_length=50, default='root')
    remark = models.TextField('备注', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = '服务器'
        verbose_name_plural = '服务器'
        db_table = 'asset_server'

    def __str__(self):
        return f'{self.name} ({self.ip})'


class ServerService(models.Model):
    """主机-服务类型关联：记录每台主机的服务规划与部署状态。

    状态语义：
      - planned  已规划（可部署）：部署前必须先落这条记录
      - deployed 已部署：部署完成后由 planned 流转而来
      - offline  已下线：下线不删记录，保留追溯链

    合法流转：planned→deployed、deployed→offline、offline→planned（重新规划）、
    deployed→planned（误标记回退）；取消规划 = 删除 planned 记录（其余状态禁止删除）。
    """

    STATUS_CHOICES = [
        ('planned', '已规划'),
        ('deployed', '已部署'),
        ('offline', '已下线'),
    ]
    # 状态 → 允许流转到的目标状态
    STATUS_TRANSITIONS = {
        'planned': {'deployed'},
        'deployed': {'offline', 'planned'},
        'offline': {'planned'},
    }

    server = models.ForeignKey(
        Server, verbose_name='服务器', on_delete=models.CASCADE, related_name='services',
    )
    # PROTECT：服务类型仍被主机引用时禁止删除，与其他字典保护策略一致
    service_type = models.ForeignKey(
        ServiceType, verbose_name='服务类型', on_delete=models.PROTECT, related_name='server_links',
    )
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='planned')
    # 进程规划：进程名规划时自动生成（<服务类型code>-<两位序号>），进程数手动维护
    process_name = models.CharField('进程名', max_length=100, blank=True)
    process_count = models.PositiveIntegerField('进程数', default=1)
    # 各状态时间戳记录「最近一次进入该状态」的时间
    planned_at = models.DateTimeField('规划时间', auto_now_add=True)
    deployed_at = models.DateTimeField('部署时间', null=True, blank=True)
    offline_at = models.DateTimeField('下线时间', null=True, blank=True)
    remark = models.CharField('备注', max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = '主机服务'
        verbose_name_plural = '主机服务'
        db_table = 'asset_server_service'
        # 同一主机同一服务类型仅一条记录，状态在记录上流转
        constraints = [
            models.UniqueConstraint(fields=['server', 'service_type'], name='uniq_server_service_type'),
        ]
        ordering = ['server_id', 'service_type_id']

    # 进程名序号的分组维度：相同云厂商+项目组+环境+渠道内，同服务类型独立编号
    PROCESS_SCOPE_FIELDS = ('cloud_provider_id', 'project_group_id', 'environment_id', 'channel_id')

    def __str__(self):
        return f'{self.server_id}-{self.service_type_id} {self.get_status_display()}'

    @classmethod
    def next_process_name(cls, server, service_type, reserved=None):
        """生成进程名：``<服务类型code>-<两位序号>``（如 nginx-01、nginx-02）。

        序号在「相同云厂商+项目组+环境+渠道」的主机范围内按服务类型递增，
        与主机名中的序号无关；取最小未占用序号（取消规划空出的序号可复用）。
        reserved 为同批次已分配但尚未入库的进程名集合（批量创建防重）。
        """
        scope = {f'server__{field}': getattr(server, field) for field in cls.PROCESS_SCOPE_FIELDS}
        names = set(
            cls.objects.filter(service_type=service_type, **scope)
            .values_list('process_name', flat=True)
        )
        if reserved:
            names |= set(reserved)
        pattern = re.compile(rf'^{re.escape(service_type.code)}-(\d+)$')
        used = {int(m.group(1)) for name in names if (m := pattern.match(name or ''))}
        seq = 1
        while seq in used:
            seq += 1
        return f'{service_type.code}-{seq:02d}'

    @classmethod
    def build_planned_links(cls, server_type_pairs, process_count=1):
        """为 (server, service_type) 组合批量构造 planned 记录，自动分配进程名。

        同批次内按「进程名分组维度 + 服务类型」隔离预留序号，避免 bulk_create
        尚未入库时同组重复取号。返回未保存的实例列表，由调用方 bulk_create。
        """
        reserved = {}
        links = []
        for server, service_type in server_type_pairs:
            key = (service_type.id, *(getattr(server, f) for f in cls.PROCESS_SCOPE_FIELDS))
            name = cls.next_process_name(server, service_type, reserved=reserved.get(key))
            reserved.setdefault(key, set()).add(name)
            links.append(cls(
                server=server, service_type=service_type, status='planned',
                process_name=name, process_count=process_count,
            ))
        return links
