"""ops-agent 接入：Agent 节点与下发任务单元。

AgentNode：NAT 出口主机上部署的执行代理，经 WebSocket（/ws/agent/）主动
外连本服务；库中仅存 token 哈希，明文只在创建/重置时展示一次。

AgentJob：runner 下发给 agent 的「模块执行单元」（一个任务流模块中路由到
某 agent 的主机组），记录 payload、回传日志与终态；与 task_exec 仅以
task_id/log_id 弱关联（整型字段，不建外键），避免 app 间耦合。

AgentRoute：按维度（项目组 + 可选环境）把主机默认路由到某个 agent，供
Server.channel_mode='auto' 的主机继承；解析口径见 channel.py。
"""
import uuid
from datetime import timedelta

from django.db import models
from django.db.models import Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from common.secrets import generate_secret, hash_secret

#: token 哈希口径：sha256 hex。实现在 common/secrets.py（与 API 凭证共用一份），
#: 这里保留别名是因为 consumers/serializers/tests 都在按这个名字导入。
hash_token = hash_secret


class AgentNode(models.Model):
    STATUS_CHOICES = [
        ('online', '在线'),
        ('offline', '离线'),
    ]

    # 心跳新鲜度阈值：status 由 consumer 的 disconnect 置 offline，但 kill -9/掉电/
    # 网络黑洞时 disconnect 根本不会跑，库里会留一个假的 online。判「是否真的在」
    # 只能看心跳时间。agent 端心跳周期远小于此值。
    HEARTBEAT_TIMEOUT = timedelta(minutes=3)

    name = models.CharField('名称', max_length=64, unique=True)
    token_hash = models.CharField('令牌哈希', max_length=64, editable=False)
    enabled = models.BooleanField('启用', default=True)
    status = models.CharField('状态', max_length=16, choices=STATUS_CHOICES, default='offline')
    last_heartbeat = models.DateTimeField('最近心跳', null=True, blank=True)
    version = models.CharField('Agent 版本', max_length=32, blank=True)
    # 心跳快照：hostname / running_jobs / load1 / disk_free_pct 等
    meta = models.JSONField('心跳快照', default=dict, blank=True)
    # 当前连接的 channel_name：踢旧连接与定向下发用，断线清空
    channel_name = models.CharField(max_length=255, blank=True, editable=False)
    remark = models.CharField('备注', max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Agent 节点'
        verbose_name_plural = 'Agent 节点'
        db_table = 'agent_node'
        ordering = ['name']

    def __str__(self):
        return self.name

    @property
    def stale(self):
        """心跳已过期：status 仍是 online 也不能认为它在线。"""
        if not self.last_heartbeat:
            return True
        return timezone.now() - self.last_heartbeat > self.HEARTBEAT_TIMEOUT

    @property
    def is_offline(self):
        return self.status != 'online' or self.stale

    @property
    def group_name(self):
        """channel layer 组名：runner 经该组向 agent 定向下发。"""
        return f'agent_{self.pk}'

    @classmethod
    def generate_token(cls):
        """生成明文 token（仅展示一次）与入库哈希。"""
        raw = generate_secret()
        return raw, hash_token(raw)


class AgentJob(models.Model):
    """一次下发单元的完整生命周期记录，兼作日志回传缓冲。

    状态机：pending（已创建未下发）→ sent（已下发）→ running（agent 已受理）
    → done（收到终态汇总，rc 表示成败）；failed = agent 拒收/离线/等待超时
    等通道层失败（rc 同步置非零）。
    """
    STATUS_CHOICES = [
        ('pending', '待下发'),
        ('sent', '已下发'),
        ('running', '执行中'),
        ('done', '已完成'),
        ('failed', '通道失败'),
    ]
    TERMINAL_STATUSES = ('done', 'failed')

    job_id = models.UUIDField('任务单元 ID', default=uuid.uuid4, unique=True, editable=False)
    agent = models.ForeignKey(
        AgentNode, verbose_name='Agent', on_delete=models.CASCADE, related_name='jobs')
    # task_exec 弱关联（不建外键）：任务流 / 执行日志 / 模块序号
    task_id = models.IntegerField('任务流 ID', null=True, blank=True)
    log_id = models.IntegerField('执行日志 ID', null=True, blank=True)
    step_order = models.IntegerField('模块序号', null=True, blank=True)
    payload = models.JSONField('下发内容', default=dict)
    status = models.CharField('状态', max_length=16, choices=STATUS_CHOICES, default='pending')
    return_code = models.IntegerField('返回码', null=True, blank=True)
    output = models.TextField('执行输出', blank=True, default='')
    # 已入库的最大日志分片序号：append 幂等去重 + 断线补传游标
    chunk_seq = models.IntegerField('日志分片游标', default=0)
    # 终态汇总：agent 上报的 host_results / elapsed 等
    result = models.JSONField('终态汇总', default=dict, blank=True)
    error = models.CharField('通道错误', max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Agent 任务单元'
        verbose_name_plural = 'Agent 任务单元'
        db_table = 'agent_job'
        ordering = ['-id']
        indexes = [models.Index(fields=['agent', 'status'])]

    def __str__(self):
        return f'{self.agent.name}#{self.job_id}'


class AgentRoute(models.Model):
    """执行通道路由规则：项目组（必填）+ 环境（可空=该项目组默认）→ agent。

    只对 Server.channel_mode='auto' 的主机生效，主机侧 direct/agent 优先级更高
    （解析见 channel.py）。规则维度刻意只做两维且项目组必填：两级回落无歧义，
    无需优先级字段与重叠检测，唯一性由下面两条带条件的约束保证
    （SQL 中 NULL != NULL，环境为空的「项目组默认」规则必须单独约束）。
    """

    agent = models.ForeignKey(
        AgentNode, verbose_name='Agent', on_delete=models.CASCADE, related_name='routes')
    project_group = models.ForeignKey(
        'asset_mgr.ProjectGroup', verbose_name='项目组', on_delete=models.PROTECT,
        related_name='agent_routes',
    )
    # 留空 = 该项目组下所有环境的默认规则（被同项目组的精确环境规则覆盖）
    environment = models.ForeignKey(
        'asset_mgr.Environment', verbose_name='环境', on_delete=models.PROTECT,
        null=True, blank=True, related_name='agent_routes',
    )
    enabled = models.BooleanField('启用', default=True)
    remark = models.CharField('备注', max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Agent 通道路由'
        verbose_name_plural = 'Agent 通道路由'
        db_table = 'agent_route'
        ordering = ['project_group_id', 'environment_id', 'id']
        constraints = [
            # 同一项目组下：精确环境的规则唯一，environment 留空的默认规则也只能有一条。
            # ⚠ 不要改回 condition=... 的条件唯一约束：MySQL 不支持，
            # Django 在该后端**直接跳过不建**（makemigrations/migrate 都不报错，
            # 只在 check 里留一条 models.W036），生产上等于没有这条约束，
            # 唯一性只剩应用层校验，并发下挡不住重复行。
            # 改用把 NULL 折叠成 0 的表达式唯一：自增主键从 1 起，0 不会与真实
            # ID 相撞；MySQL 8.0.13+ 的函数索引支持它，SQLite 也支持表达式索引。
            models.UniqueConstraint(
                'project_group',
                Coalesce('environment', Value(0), output_field=models.BigIntegerField()),
                name='uniq_agent_route_pg_env',
            ),
        ]

    def __str__(self):
        env = self.environment.name if self.environment_id else '不限环境'
        return f'{self.project_group.name}/{env} → {self.agent.name}'
