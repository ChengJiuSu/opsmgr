"""ops-agent 接入：Agent 节点与下发任务单元。

AgentNode：NAT 出口主机上部署的执行代理，经 WebSocket（/ws/agent/）主动
外连本服务；库中仅存 token 哈希，明文只在创建/重置时展示一次。

AgentJob：runner 下发给 agent 的「模块执行单元」（一个任务流模块中路由到
某 agent 的主机组），记录 payload、回传日志与终态；与 task_exec 仅以
task_id/log_id 弱关联（整型字段，不建外键），避免 app 间耦合。
"""
import hashlib
import secrets
import uuid

from django.db import models


def hash_token(raw):
    """token 哈希口径：sha256 hex，全项目唯一实现。"""
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


class AgentNode(models.Model):
    STATUS_CHOICES = [
        ('online', '在线'),
        ('offline', '离线'),
    ]

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
    def group_name(self):
        """channel layer 组名：runner 经该组向 agent 定向下发。"""
        return f'agent_{self.pk}'

    @classmethod
    def generate_token(cls):
        """生成明文 token（仅展示一次）与入库哈希。"""
        raw = secrets.token_urlsafe(32)
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
