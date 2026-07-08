from django.conf import settings
from django.db import models


class Credential(models.Model):
    """主机连接凭据。密钥/密码字段仅用于下发 Ansible 执行，API 不回显。"""

    AUTH_CHOICES = [
        ('password', '密码'),
        ('key', '私钥'),
    ]

    name = models.CharField('凭据名称', max_length=100, unique=True)
    username = models.CharField('登录用户', max_length=50, default='root')
    auth_type = models.CharField('认证方式', max_length=20, choices=AUTH_CHOICES, default='password')
    password = models.CharField('密码', max_length=255, blank=True)
    private_key = models.TextField('私钥', blank=True)
    become = models.BooleanField('提权 (sudo)', default=False)
    become_password = models.CharField('提权密码', max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = '凭据'
        verbose_name_plural = '凭据'
        db_table = 'task_credential'
        ordering = ['name']

    def __str__(self):
        return self.name


class ExecutionTask(models.Model):
    """任务执行记录：命令 / Playbook 剧本 / 平台方法。

    三类任务的执行主机统一来自平台服务器台账（按项目/渠道/环境筛选、
    仅限已规划服务的主机），执行时动态生成 inventory；平台方法仅承载
    运维后台能力（provider 化的既有剧本 tags、云主机管理等）。
    """

    TYPE_CHOICES = [
        ('command', '命令'),
        ('playbook', 'Playbook'),
        ('method', '平台方法'),
    ]
    STATUS_CHOICES = [
        ('pending', '等待中'),
        ('running', '执行中'),
        ('success', '成功'),
        ('failed', '失败'),
    ]

    name = models.CharField('任务名称', max_length=150)
    task_type = models.CharField('任务类型', max_length=20, choices=TYPE_CHOICES, default='command')

    # ── 目标主机与连接（三类任务共用，执行时动态生成 inventory）──
    servers = models.ManyToManyField(
        'asset_mgr.Server', verbose_name='目标主机', related_name='exec_tasks', blank=True,
    )
    credential = models.ForeignKey(
        Credential, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='tasks', verbose_name='连接凭据',
    )

    # ── 命令任务（ad-hoc shell）──
    command = models.TextField('Shell 命令', blank=True)
    pattern = models.CharField('主机匹配', max_length=100, default='all')

    # ── Playbook 任务（ANSIBLE_PROJECT_DIR 内相对路径）──
    playbook = models.CharField('剧本路径', max_length=500, blank=True)

    # ── 平台方法任务（通过 method_mgr provider 执行）──
    method = models.ForeignKey(
        'method_mgr.Method', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='tasks', verbose_name='平台方法',
    )

    # ── Ansible 参数（playbook / 方法任务共用；tags/limit 覆盖方法默认值）──
    tags = models.CharField('Tags 覆盖', max_length=255, blank=True)
    limit = models.CharField('Limit 主机', max_length=255, blank=True)
    extra_vars = models.TextField('额外变量', blank=True)
    params = models.JSONField('方法参数', default=dict, blank=True)  # 预留

    timeout = models.PositiveIntegerField('超时(秒)', default=300)
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    return_code = models.IntegerField('返回码', null=True, blank=True)
    output = models.TextField('执行输出', blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='exec_tasks', verbose_name='发起人',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField('开始时间', null=True, blank=True)
    finished_at = models.DateTimeField('结束时间', null=True, blank=True)

    class Meta:
        verbose_name = '执行任务'
        verbose_name_plural = '执行任务'
        db_table = 'task_execution'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.name} ({self.get_task_type_display()})'

    @property
    def duration(self):
        if self.started_at and self.finished_at:
            return round((self.finished_at - self.started_at).total_seconds(), 2)
        return None


class TaskExecutionLog(models.Model):
    """任务执行日志：任务每执行一次落一条记录，重跑追加而不覆盖历史。

    任务本体（ExecutionTask）上的状态/输出字段仅作为「最近一次执行」快照；
    完整执行历史以本表为准。输出超过预览上限时全量落盘（settings.TASK_LOG_DIR），
    ``log_file`` 保存相对该目录的文件路径。
    """

    TRIGGER_CHOICES = [
        ('create', '创建执行'),
        ('rerun', '重跑'),
    ]

    task = models.ForeignKey(
        ExecutionTask, on_delete=models.CASCADE,
        related_name='logs', verbose_name='所属任务',
    )
    trigger_type = models.CharField('触发方式', max_length=20, choices=TRIGGER_CHOICES, default='create')
    triggered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='exec_task_logs', verbose_name='触发人',
    )
    status = models.CharField('状态', max_length=20, choices=ExecutionTask.STATUS_CHOICES, default='pending')
    return_code = models.IntegerField('返回码', null=True, blank=True)
    output = models.TextField('输出预览', blank=True)
    log_file = models.CharField('完整日志文件（相对 TASK_LOG_DIR）', max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField('开始时间', null=True, blank=True)
    finished_at = models.DateTimeField('结束时间', null=True, blank=True)

    class Meta:
        verbose_name = '任务执行日志'
        verbose_name_plural = '任务执行日志'
        db_table = 'task_execution_log'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.task_id}#{self.pk} {self.get_status_display()}'

    @property
    def duration(self):
        if self.started_at and self.finished_at:
            return round((self.finished_at - self.started_at).total_seconds(), 2)
        return None
