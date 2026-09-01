"""拓扑画布数据模型：把「项目组 × 渠道 × 环境」下的接入链路画成一张图。

## 为什么画布不是新数据源

画布是**视图 + 关系层**，不是第五套资产台账。节点分两类，判据是「平台有没有这个
东西的模型」：

- `ref` 引用型：指向平台已有实体（服务器 / 主域名 / 解析记录 / 证书），只存一个
  `kind + ref_id`，名称、IP、状态一律**实时读**原表。刻意不复制快照——主机改了名、
  证书快过期，画布上必须当场看见；存快照就变成又一份会过期的台账，而运维看图的
  目的正是「看现在长什么样」。实体被删则节点标为已失联（`ref.exists=False`），
  不静默消失，否则图会莫名断链且没人知道断在哪。
- `external` 外部型：GA 加速器、各家负载均衡、CDN 这些平台尚未建模的云资源，
  手工录入 `label + attrs`。将来 cloud_mgr 补上 LoadBalancer 模型，这类节点可就地
  升级成引用型（改 source/kind/ref_id 即可，边不用动）。

## 一画布 = 一个维度组合下的一张图

维度口径与 CMDB 一致（ProjectGroup / Channel / Environment，region 为可选自由
文本，留空 = 跨地域）。同一组合允许多张图，用 `slug` 区分（`ingress-path`、
`db-topology`）：一张图塞两件事既排不开版也没人看得懂。

## 边是关系，不只是连线

`relation` 带语义（解析 / 后端 / 源站 / 回源 / 落地 / 挂载），所以「这台机下线会
影响哪些域名」可以靠边反查（见 views.ImpactView）。如果边只是装饰线条，画布就只是
一张画，救不了任何一次故障。
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

#: 引用型节点的 kind：取值即被引用的平台实体类型，`kind` 与 `ref_type` 合成一个
#: 字段（二者永远相等，拆两个字段只是给「写歪了」留位置）。
REF_KINDS = {
    'server': '服务器',
    'domain': '主域名',
    'dns_record': '解析记录',
    'certificate': '证书',
}

#: 外部型节点的 kind，按云厂商分组 —— 前端左侧调色板直接按这个结构渲染，
#: 不在前端再写死一份（加一种 LB 只改这里）。
EXTERNAL_KIND_GROUPS = (
    ('接入层', (
        ('accel_domain', '加速域名'),
        ('cdn', 'CDN'),
        ('waf', 'WAF'),
    )),
    ('AWS', (
        ('aws_ga', 'GA 加速器'),
        ('aws_alb', '负载均衡 ALB'),
        ('aws_nlb', '负载均衡 NLB'),
        ('aws_cloudfront', 'CloudFront'),
    )),
    ('GCP', (
        ('gcp_lb', '负载均衡'),
        ('gcp_cdn', 'Cloud CDN'),
    )),
    ('腾讯云', (
        ('tencent_clb', '负载均衡 CLB'),
        ('tencent_cdn', 'CDN'),
    )),
    ('阿里云', (
        ('aliyun_slb', '负载均衡 SLB'),
        ('aliyun_cdn', 'CDN'),
    )),
    ('其他', (
        ('custom', '自定义节点'),
    )),
)

EXTERNAL_KINDS = {
    code: label for _, members in EXTERNAL_KIND_GROUPS for code, label in members
}
ALL_KINDS = {**REF_KINDS, **EXTERNAL_KINDS}

#: 边的关系语义。`custom` 是兜底（画得出来但说不清是什么关系的连线），
#: 留它是为了不逼人把「暂时说不清」硬塞进一个错的语义里。
RELATIONS = (
    ('resolve', '解析'),
    ('backend', '后端'),
    ('origin', '源站'),
    ('upstream', '回源'),
    ('landing', '落地'),
    ('member', '挂载'),
    ('custom', '关联'),
)
RELATION_LABELS = dict(RELATIONS)

#: 连线可以挂在节点卡片的哪个连接点上。空串 = 没指定，前端按默认（右出左进）画。
#: 四个方向都开是为了排版：一条竖着的关系（解析记录 → 证书）若只能右出左进，
#: 线会绕一个大弯从卡片侧面兜过去，图上看着像连错了
HANDLES = ('', 'left', 'right', 'top', 'bottom')


class TopoCanvas(models.Model):
    """一张拓扑画布。"""

    LAYOUT_CHOICES = [
        ('manual', '手工布局'),
        ('auto', '自动排版'),
    ]

    name = models.CharField('画布名称', max_length=100)
    slug = models.SlugField(
        '画布标识', max_length=50,
        help_text='同一维度组合下区分多张画布，如 ingress-path')
    # 维度字典一律 PROTECT：画布引用着它们，删字典前应先处理画布
    project_group = models.ForeignKey(
        'asset_mgr.ProjectGroup', verbose_name='项目组', on_delete=models.PROTECT,
        related_name='topo_canvases',
    )
    channel = models.ForeignKey(
        'asset_mgr.Channel', verbose_name='渠道', on_delete=models.PROTECT,
        related_name='topo_canvases',
    )
    environment = models.ForeignKey(
        'asset_mgr.Environment', verbose_name='环境', on_delete=models.PROTECT,
        related_name='topo_canvases',
    )
    # 地域：Server.region 目前是自由文本，这里保持同口径（留空 = 跨地域）。
    # 等 region 进字典再一起改成外键，现在造一个外键会与主机上的文本对不上
    region = models.CharField('地域', max_length=50, blank=True)
    description = models.TextField('说明', blank=True)
    layout_mode = models.CharField('布局模式', max_length=10,
                                   choices=LAYOUT_CHOICES, default='manual')
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name='创建人', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='topo_canvases',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = '拓扑画布'
        verbose_name_plural = '拓扑画布'
        db_table = 'topo_canvas'
        ordering = ['project_group_id', 'channel_id', 'environment_id', 'slug']
        constraints = [
            models.UniqueConstraint(
                fields=['project_group', 'channel', 'environment', 'region', 'slug'],
                name='uniq_topo_canvas_dims_slug',
            ),
        ]

    def __str__(self):
        return f'{self.name}（{self.dim_label}）'

    @property
    def dim_label(self):
        """维度描述，报错与审计里指名道姓，省一次翻页面。"""
        parts = [self.project_group.code, self.channel.code, self.environment.code]
        if self.region:
            parts.append(self.region)
        return '/'.join(parts)


class TopoNode(models.Model):
    """画布上的一个节点。"""

    SOURCE_CHOICES = [
        ('ref', '平台资源'),
        ('external', '外部资源'),
    ]

    canvas = models.ForeignKey(
        TopoCanvas, verbose_name='画布', on_delete=models.CASCADE, related_name='nodes')
    source = models.CharField('节点来源', max_length=10,
                             choices=SOURCE_CHOICES, default='external')
    # 不做 choices：节点类型会随「又接入一家云」增长，choices 意味着每加一种都要
    # 一条迁移。合法值由 clean() 按 ALL_KINDS 校验，写歪了在保存时就报错
    kind = models.SlugField('节点类型', max_length=32)
    ref_id = models.PositiveIntegerField('引用实体 ID', null=True, blank=True)
    # 引用型节点也存一份 label：实体被删后图上仍能显示「它原来是谁」，
    # 未失联时展示一律以实时读到的名字为准（见 graph.build_graph）
    label = models.CharField('显示名称', max_length=200)
    sub_label = models.CharField('副标题', max_length=200, blank=True)
    # 图上这个节点自己的展示细节，自由字段。当前被前端识别的键：
    #   kind_label —— 覆盖节点卡片上那行类型标签（默认取 EXTERNAL_KIND_GROUPS /
    #                 REF_KINDS 里登记的名字）。只影响展示，真实类型始终是 kind
    # 加新键前先看一眼这里，别把已有语义的键改了用途
    attrs = models.JSONField('自定义属性', default=dict, blank=True)
    x = models.FloatField('画布 X 坐标', default=0)
    y = models.FloatField('画布 Y 坐标', default=0)
    # 钉住的节点自动排版不动它：人手调过的位置通常带着「这条链路要横着看」这类
    # 说不出口的信息，被一次自动排版抹掉会很恼人
    pinned = models.BooleanField('固定位置', default=False)
    remark = models.CharField('备注', max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = '拓扑节点'
        verbose_name_plural = '拓扑节点'
        db_table = 'topo_node'
        ordering = ['id']
        constraints = [
            # 同一张画布里同一个平台实体只允许出现一次：出现两次的图会让「影响面
            # 反查」数出两条链路，而实际只有一条
            models.UniqueConstraint(
                fields=['canvas', 'kind', 'ref_id'],
                condition=models.Q(ref_id__isnull=False),
                name='uniq_topo_node_ref',
            ),
            # source 与 ref_id 必须自洽：外部节点带着 ref_id 会被当成引用型去查
            # 实体，引用型缺 ref_id 则永远查不到实体
            models.CheckConstraint(
                condition=(
                    models.Q(source='ref', ref_id__isnull=False)
                    | models.Q(source='external', ref_id__isnull=True)
                ),
                name='ck_topo_node_source_ref',
            ),
        ]
        indexes = [
            # 影响面反查：给定实体找它在哪些画布上
            models.Index(fields=['kind', 'ref_id']),
        ]

    def __str__(self):
        return f'{ALL_KINDS.get(self.kind, self.kind)}·{self.label}'

    @property
    def kind_label(self):
        return ALL_KINDS.get(self.kind, self.kind)

    def clean(self):
        if self.source == 'ref':
            if self.kind not in REF_KINDS:
                raise ValidationError(
                    f'「{self.kind}」不是可引用的平台资源类型；'
                    f'可选：{"、".join(REF_KINDS)}')
            if not self.ref_id:
                raise ValidationError('引用平台资源的节点必须指定 ref_id')
        else:
            if self.kind not in EXTERNAL_KINDS:
                raise ValidationError(
                    f'「{self.kind}」不是可手工录入的节点类型；'
                    f'可选：{"、".join(EXTERNAL_KINDS)}')
            if self.ref_id:
                raise ValidationError('外部资源节点不能携带 ref_id')
        if not self.label:
            raise ValidationError('节点显示名称不能为空')


class TopoEdge(models.Model):
    """节点之间的一条有向关系。"""

    canvas = models.ForeignKey(
        TopoCanvas, verbose_name='画布', on_delete=models.CASCADE, related_name='edges')
    source = models.ForeignKey(
        TopoNode, verbose_name='起点', on_delete=models.CASCADE, related_name='out_edges')
    target = models.ForeignKey(
        TopoNode, verbose_name='终点', on_delete=models.CASCADE, related_name='in_edges')
    relation = models.CharField('关系', max_length=16, choices=RELATIONS, default='custom')
    # 连接点：连线从起点的哪一侧出、进终点的哪一侧。纯排版信息，不影响任何语义判定，
    # 但必须落库——不存的话保存一次，人手调好的走线全部弹回左右两侧
    source_handle = models.CharField('起点连接点', max_length=10, blank=True)
    target_handle = models.CharField('终点连接点', max_length=10, blank=True)
    label = models.CharField('连线文字', max_length=50, blank=True)
    attrs = models.JSONField('自定义属性', default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = '拓扑连线'
        verbose_name_plural = '拓扑连线'
        db_table = 'topo_edge'
        ordering = ['id']
        constraints = [
            models.UniqueConstraint(
                fields=['canvas', 'source', 'target', 'relation'],
                name='uniq_topo_edge',
            ),
            models.CheckConstraint(
                condition=~models.Q(source=models.F('target')),
                name='ck_topo_edge_no_self_loop',
            ),
        ]

    def __str__(self):
        return f'{self.source_id} -[{self.relation}]-> {self.target_id}'

    def clean(self):
        if self.source_id and self.source_id == self.target_id:
            raise ValidationError('连线的起点与终点不能是同一个节点')
        for field, value in (('起点', self.source_handle), ('终点', self.target_handle)):
            if value not in HANDLES:
                raise ValidationError(
                    f'{field}连接点「{value}」非法；可选：'
                    f'{"、".join(h or "（默认）" for h in HANDLES)}')
        for field, node in (('起点', self.source), ('终点', self.target)):
            if node is not None and node.canvas_id != self.canvas_id:
                raise ValidationError(f'连线{field}不在本画布上')
