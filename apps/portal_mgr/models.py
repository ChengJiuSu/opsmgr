"""内部导航页（/nav.html）的配置数据模型。

导航页原先由页面内的 JS 数组驱动，改配置要动前端文件；这里把整份配置搬进
「配置管理 → 内部导航页」，由页面启动时调 `/api/portal/config/` 拉取。

刻意保留「导航列」与「内容区块」两套结构而不是合成一张表：侧栏想少列一项、
换个顺序或挂个纯外链，都不该牵动右侧内容；两者只通过 PortalNavItem.block
关联，且允许为空（外链项）。这与页面改造时定下的口径一致。
"""

from django.core.exceptions import ValidationError
from django.db import models


class PortalSetting(models.Model):
    """导航页外观与文案，全局单例（固定 pk=1，由数据迁移写入）。"""

    SINGLETON_PK = 1

    # ── 布局 ──
    sidebar_width = models.PositiveSmallIntegerField('侧栏宽度(px)', default=272)
    max_cols = models.PositiveSmallIntegerField('卡片网格列数上限', default=4)
    recent_max = models.PositiveSmallIntegerField('最近访问保留条数', default=8)
    masthead_open_by_default = models.BooleanField('首次访问展开报头', default=True)

    # ── 品牌区 ──
    brand_mark = models.CharField('方形标字符', max_length=4, blank=True, default='N')
    brand_name = models.CharField('站点名称', max_length=50, default='内部系统导航台')
    brand_sub = models.CharField('英文副标', max_length=80, blank=True,
                                 default='Internal Systems Index')

    # ── 报头 ──
    masthead_eyebrow = models.CharField('眉标', max_length=80, blank=True)
    masthead_title = models.TextField(
        '主标题', blank=True,
        help_text='每行一行标题，换行即换行显示')
    masthead_intro = models.TextField('说明文字', blank=True)
    masthead_systems_label = models.CharField('统计块标签·系统总数', max_length=20,
                                              default='系统总数')
    masthead_groups_label = models.CharField('统计块标签·类别数', max_length=20,
                                             default='一级类别')

    # ── 侧栏底部 ──
    footer = models.TextField(
        '底部说明', blank=True,
        help_text='每行一条；用 **文字** 包裹的部分加粗显示')

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = '导航页设置'
        verbose_name_plural = '导航页设置'
        db_table = 'portal_setting'

    def __str__(self):
        return self.brand_name

    def save(self, *args, **kwargs):
        # 单例：始终落在固定主键上，避免出现第二份「生效中」的配置
        self.pk = self.SINGLETON_PK
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('导航页设置为全局单例，不可删除')

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=cls.SINGLETON_PK)
        return obj


class PortalBlock(models.Model):
    """内容区块：右侧主区按 sort_order 从上到下渲染的分组。"""

    code = models.SlugField('编码', max_length=50, unique=True,
                            help_text='导航项与系统清单据此关联，建后不建议再改')
    title = models.CharField('区块标题', max_length=50)
    tag = models.CharField('归类标签', max_length=50, blank=True,
                           help_text='显示在区块标题右侧的小号灰字')
    enabled = models.BooleanField('是否启用', default=True)
    sort_order = models.PositiveIntegerField('排序', default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = '导航页内容区块'
        verbose_name_plural = '导航页内容区块'
        db_table = 'portal_block'
        ordering = ['sort_order', 'id']

    def __str__(self):
        return self.title


class PortalSystem(models.Model):
    """系统清单：导航页上的一张卡片。"""

    block = models.ForeignKey(PortalBlock, on_delete=models.PROTECT,
                              related_name='systems', verbose_name='所属区块')
    name = models.CharField('系统名称', max_length=50)
    abbr = models.CharField('缩写标', max_length=6,
                            help_text='卡片左侧方标内的字符，建议 2-4 位')
    description = models.CharField('描述', max_length=200, blank=True,
                                   help_text='卡片上最多显示两行，超出截断')
    domain = models.CharField('域名', max_length=100,
                              help_text='卡片底部小号大写显示，同时作为去重标识')
    url = models.URLField('访问地址', max_length=300)
    enabled = models.BooleanField('是否启用', default=True)
    sort_order = models.PositiveIntegerField('排序', default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = '导航页系统'
        verbose_name_plural = '导航页系统'
        db_table = 'portal_system'
        ordering = ['sort_order', 'id']
        constraints = [
            # 域名是页面上的卡片标识（置顶与最近访问按它记 localStorage），必须唯一
            models.UniqueConstraint(fields=['domain'], name='portal_system_domain_unique'),
        ]

    def __str__(self):
        return self.name


class PortalNavGroup(models.Model):
    """导航列分组：侧栏的一级类别。"""

    name = models.CharField('分组名称', max_length=50)
    en_name = models.CharField('英文名', max_length=50, blank=True)
    enabled = models.BooleanField('是否启用', default=True)
    sort_order = models.PositiveIntegerField('排序', default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = '导航页分组'
        verbose_name_plural = '导航页分组'
        db_table = 'portal_nav_group'
        ordering = ['sort_order', 'id']

    def __str__(self):
        return self.name


class PortalNavItem(models.Model):
    """导航项：指向一个内容区块，或指向一个纯外链。

    两者二选一，由 clean() 保证——都填或都不填，页面上都会渲染成一个点了没反应
    的条目，属于配得出来但不成立的状态，拦在写入层。
    """

    group = models.ForeignKey(PortalNavGroup, on_delete=models.CASCADE,
                              related_name='items', verbose_name='所属分组')
    label = models.CharField('显示名称', max_length=50, blank=True,
                             help_text='留空则取所指区块的标题')
    block = models.ForeignKey(PortalBlock, on_delete=models.PROTECT,
                              null=True, blank=True, related_name='nav_items',
                              verbose_name='指向区块')
    href = models.URLField('外链地址', max_length=300, blank=True,
                           help_text='填了外链就不要再选区块，两者二选一')
    enabled = models.BooleanField('是否启用', default=True)
    sort_order = models.PositiveIntegerField('排序', default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = '导航项'
        verbose_name_plural = '导航项'
        db_table = 'portal_nav_item'
        ordering = ['sort_order', 'id']

    def __str__(self):
        return self.label or (self.block.title if self.block else self.href)

    def clean(self):
        if bool(self.block) == bool(self.href):
            raise ValidationError('导航项需要且只能二选一：指向区块，或填写外链地址')
        if self.href and not self.label:
            raise ValidationError('外链导航项必须填写显示名称')


class PortalNotice(models.Model):
    """维护公告：生效窗口内进入导航页自动弹窗。"""

    code = models.SlugField('公告编号', max_length=50, unique=True,
                            help_text='如 2026-08-CHG-114；「不再提示本条」按它记住')
    title = models.CharField('标题', max_length=100)
    systems = models.ManyToManyField(PortalSystem, blank=True,
                                     related_name='notices', verbose_name='影响系统')
    start_at = models.DateTimeField('开始时间')
    end_at = models.DateTimeField('结束时间')
    body = models.TextField('正文', help_text='换行会原样保留')
    enabled = models.BooleanField('是否启用', default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = '维护公告'
        verbose_name_plural = '维护公告'
        db_table = 'portal_notice'
        ordering = ['-start_at', '-id']

    def __str__(self):
        return self.title

    def clean(self):
        if self.start_at and self.end_at and self.start_at >= self.end_at:
            raise ValidationError('结束时间必须晚于开始时间')
