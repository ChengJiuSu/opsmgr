from django.conf import settings
from django.db import models


def match_executable_servers(project_group_id=None, channel_id=None, environment_id=None):
    """按维度匹配可执行主机（已规划/已部署服务，维度为 None=不限）。

    任务全局范围与模块自定义范围共用；与服务器接口 executable=true 及
    runner 执行权限校验保持同一口径。
    """
    from apps.asset_mgr.models import Server

    qs = (
        Server.objects
        .filter(services__status__in=('planned', 'deployed'))
        .distinct()
        .select_related('project_group', 'channel', 'environment', 'cloud_provider')
        .prefetch_related('services__service_type')
        .order_by('name', 'id')
    )
    if project_group_id:
        qs = qs.filter(project_group_id=project_group_id)
    if channel_id:
        qs = qs.filter(channel_id=channel_id)
    if environment_id:
        qs = qs.filter(environment_id=environment_id)
    return qs


class ExecVariable(models.Model):
    """执行变量：任务执行时按模块有效维度（项目组/渠道/环境）加载，
    变量值为 YAML 文本，解析后以 --extra-vars 注入三类模块（命令 Jinja
    引用 / playbook / 平台方法），模块自身的额外变量后置覆盖同名项。

    每次保存/回滚落一条版本快照（ExecVariableVersion，默认保留 10 版）。
    同时作为对外数据源：后续 Jenkins 等外部系统可经 API 获取变量列表，
    配合服务器接口（executable=true）自行拼装 ansible 调用（规划中）。
    """

    key = models.CharField('变量名', max_length=100, unique=True)
    value = models.TextField('变量值（YAML）', blank=True)
    description = models.CharField('说明', max_length=255, blank=True)
    # 生效条件：与任务/模块的有效维度精确匹配才注入（三维度必选）
    project_group = models.ForeignKey(
        'asset_mgr.ProjectGroup', verbose_name='项目组', on_delete=models.PROTECT,
        null=True, blank=True, related_name='exec_variables',
    )
    channel = models.ForeignKey(
        'asset_mgr.Channel', verbose_name='渠道', on_delete=models.PROTECT,
        null=True, blank=True, related_name='exec_variables',
    )
    environment = models.ForeignKey(
        'asset_mgr.Environment', verbose_name='环境', on_delete=models.PROTECT,
        null=True, blank=True, related_name='exec_variables',
    )
    enabled = models.BooleanField('启用', default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = '执行变量'
        verbose_name_plural = '执行变量'
        db_table = 'task_variable'
        ordering = ['key']

    def __str__(self):
        return self.key


class ExecVariableVersion(models.Model):
    """执行变量版本快照：每次保存/回滚落一条，按变量保留最近 N 版
    （settings.EXEC_VARIABLE_VERSION_LIMIT，默认 10），支持回滚。

    维度快照用 SET_NULL 弱关联：字典项删除不影响历史版本记录。
    """

    variable = models.ForeignKey(
        ExecVariable, on_delete=models.CASCADE, related_name='versions', verbose_name='所属变量',
    )
    version = models.PositiveIntegerField('版本号')
    value = models.TextField('变量值（YAML）', blank=True)
    description = models.CharField('说明', max_length=255, blank=True)
    enabled = models.BooleanField('启用', default=True)
    project_group = models.ForeignKey(
        'asset_mgr.ProjectGroup', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    channel = models.ForeignKey(
        'asset_mgr.Channel', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    environment = models.ForeignKey(
        'asset_mgr.Environment', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+', verbose_name='操作人',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = '执行变量版本'
        verbose_name_plural = '执行变量版本'
        db_table = 'task_variable_version'
        ordering = ['-version']
        constraints = [
            models.UniqueConstraint(fields=['variable', 'version'], name='uniq_variable_version'),
        ]

    def __str__(self):
        return f'{self.variable_id}#v{self.version}'


def snapshot_variable(variable, user=None):
    """给变量落一条版本快照，并按保留上限清理旧版本。

    每次保存/回滚后调用；版本号在该变量内自增。
    """
    limit = getattr(settings, 'EXEC_VARIABLE_VERSION_LIMIT', 10)
    last = variable.versions.order_by('-version').first()
    ExecVariableVersion.objects.create(
        variable=variable,
        version=(last.version + 1) if last else 1,
        value=variable.value,
        description=variable.description,
        enabled=variable.enabled,
        project_group_id=variable.project_group_id,
        channel_id=variable.channel_id,
        environment_id=variable.environment_id,
        created_by=user,
    )
    stale = list(variable.versions.order_by('-version').values_list('id', flat=True)[limit:])
    if stale:
        ExecVariableVersion.objects.filter(id__in=stale).delete()


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
    """任务（任务流）：由多个任务模块（TaskStep）按序组成的可复用任务定义。

    新建仅保存定义；执行为独立动作：模块间顺序执行（失败中止后续），
    模块内主机按 exec_mode 并发/串行。执行主机按维度实时匹配（全局维度为
    默认，模块可自定义覆盖；仅限已规划服务的主机，维度留空=不限）。
    任务本体的状态/输出字段仅作为「最近一次执行」快照，完整历史见
    TaskExecutionLog。
    """

    STATUS_CHOICES = [
        ('pending', '等待中'),
        ('running', '执行中'),
        ('paused', '等待处理'),  # 暂停确认模块等待继续/中止，或模块失败等待重试/跳过/中止
        ('success', '成功'),
        ('failed', '失败'),
    ]
    PAUSE_TYPE_CHOICES = [
        ('confirm', '暂停确认'),   # 暂停确认模块：可 continue/abort
        ('failed', '失败待处理'),  # 模块执行失败：可 retry/skip/abort
    ]
    EXEC_MODE_CHOICES = [
        ('parallel', '并发'),
        ('serial', '串行'),
    ]

    name = models.CharField('任务名称', max_length=150)

    # ── 全局主机范围（模块默认跟随，可自定义覆盖；留空=不限）──
    project_group = models.ForeignKey(
        'asset_mgr.ProjectGroup', verbose_name='项目组', on_delete=models.PROTECT,
        null=True, blank=True, related_name='exec_tasks',
    )
    channel = models.ForeignKey(
        'asset_mgr.Channel', verbose_name='渠道', on_delete=models.PROTECT,
        null=True, blank=True, related_name='exec_tasks',
    )
    environment = models.ForeignKey(
        'asset_mgr.Environment', verbose_name='环境', on_delete=models.PROTECT,
        null=True, blank=True, related_name='exec_tasks',
    )
    # 模块内主机的执行方式：并发（默认，按 forks 分批）/ 串行（--forks 1 逐台）；
    # 模块之间的并发见 TaskStep.run_parallel
    exec_mode = models.CharField('主机执行方式', max_length=20, choices=EXEC_MODE_CHOICES, default='parallel')
    # 并发模式下的主机并发数（ansible --forks）：默认 10 台一批
    forks = models.PositiveIntegerField('主机并发数', default=10)

    # ── 目标主机与连接：servers 为「最近一次执行的全局范围主机」快照 ──
    servers = models.ManyToManyField(
        'asset_mgr.Server', verbose_name='目标主机', related_name='exec_tasks', blank=True,
    )
    credential = models.ForeignKey(
        Credential, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='tasks', verbose_name='连接凭据',
    )

    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    return_code = models.IntegerField('返回码', null=True, blank=True)
    output = models.TextField('执行输出', blank=True)
    # 暂停信号：paused 时由确认接口按 pause_type 写动作（continue/abort 或
    # retry/skip/abort），runner 轮询消费后清空
    pause_action = models.CharField('暂停处理动作', max_length=20, blank=True, default='')
    pause_type = models.CharField(
        '暂停类型', max_length=20, choices=PAUSE_TYPE_CHOICES, blank=True, default='',
    )
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
        return self.name

    @property
    def duration(self):
        if self.started_at and self.finished_at:
            return round((self.finished_at - self.started_at).total_seconds(), 2)
        return None

    def matched_servers(self):
        """全局主机范围：按任务维度实时匹配可执行主机。"""
        return match_executable_servers(self.project_group_id, self.channel_id, self.environment_id)


class TaskStep(models.Model):
    """任务模块：任务流中的一个步骤，模块级选择类型与内容。

    主机范围默认跟随任务全局维度；use_custom_scope=True 时按模块自己的
    三维度独立匹配（执行表单的主机勾选仅作用于全局范围模块）。
    """

    TYPE_CHOICES = [
        ('command', '命令'),
        ('playbook', 'Playbook'),
        ('method', '平台方法'),
        ('pause', '暂停确认'),  # 任务流暂停等待人工确认，timeout 为等待上限
    ]

    task = models.ForeignKey(
        ExecutionTask, on_delete=models.CASCADE, related_name='steps', verbose_name='所属任务',
    )
    order = models.PositiveIntegerField('执行顺序', default=1)
    name = models.CharField('模块名称', max_length=100)
    task_type = models.CharField('模块类型', max_length=20, choices=TYPE_CHOICES, default='command')

    # ── 模块内容（按类型取用）──
    command = models.TextField('Shell 命令', blank=True)
    playbook = models.CharField('剧本路径', max_length=500, blank=True)
    method = models.ForeignKey(
        'method_mgr.Method', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='steps', verbose_name='平台方法',
    )
    tags = models.CharField('Tags', max_length=255, blank=True)
    extra_vars = models.TextField('额外变量', blank=True)
    # 平台方法模块的参数（JSON 对象，预留给 provider 消费，如云主机管理）
    params = models.JSONField('方法参数', default=dict, blank=True)
    timeout = models.PositiveIntegerField('超时(秒)', default=300)
    # 模块间并行：相邻的 run_parallel=True 模块合并为一个并行组同时执行，
    # 组内全部成功才进入下一阶段；默认 False=按序串行（pause 模块禁止并行）
    run_parallel = models.BooleanField('与相邻并行模块并发', default=False)

    # ── 主机范围：默认跟随任务全局，自定义时按模块维度独立匹配 ──
    use_custom_scope = models.BooleanField('自定义主机范围', default=False)
    project_group = models.ForeignKey(
        'asset_mgr.ProjectGroup', verbose_name='项目组', on_delete=models.PROTECT,
        null=True, blank=True, related_name='task_steps',
    )
    channel = models.ForeignKey(
        'asset_mgr.Channel', verbose_name='渠道', on_delete=models.PROTECT,
        null=True, blank=True, related_name='task_steps',
    )
    environment = models.ForeignKey(
        'asset_mgr.Environment', verbose_name='环境', on_delete=models.PROTECT,
        null=True, blank=True, related_name='task_steps',
    )

    class Meta:
        verbose_name = '任务模块'
        verbose_name_plural = '任务模块'
        db_table = 'task_step'
        ordering = ['order', 'id']

    def __str__(self):
        return f'{self.task_id}#{self.order} {self.name}'

    @property
    def exec_mode(self):
        """执行方式任务级统一（方法执行器按 step 读取时透传）。"""
        return self.task.exec_mode

    @property
    def forks(self):
        """主机并发数任务级统一（方法执行器按 step 读取时透传）。"""
        return self.task.forks

    def matched_servers(self):
        """自定义范围：按模块维度实时匹配可执行主机（仅 use_custom_scope 时使用）。"""
        return match_executable_servers(self.project_group_id, self.channel_id, self.environment_id)


class TaskExecutionLog(models.Model):
    """任务执行日志：任务每执行一次落一条记录，反复执行追加而不覆盖历史。

    任务本体（ExecutionTask）上的状态/输出字段仅作为「最近一次执行」快照；
    完整执行历史以本表为准。执行期间完整输出流式落盘（settings.TASK_LOG_DIR），
    ``log_file`` 保存相对该目录的文件路径（文件过保留期被清理后置空），
    ``output`` 仅存尾部预览。
    """

    TRIGGER_CHOICES = [
        ('create', '创建执行'),  # 历史数据：曾经的「创建即执行」模式
        ('rerun', '重跑'),       # 历史数据：曾经的重跑动作
        ('execute', '手动执行'),
    ]

    task = models.ForeignKey(
        ExecutionTask, on_delete=models.CASCADE,
        related_name='logs', verbose_name='所属任务',
    )
    trigger_type = models.CharField('触发方式', max_length=20, choices=TRIGGER_CHOICES, default='execute')
    # 本次执行的范围快照：全局范围主机 [{id,name,ip}] 与执行方式（历史可追溯，不随任务编辑变化）
    servers_snapshot = models.JSONField('执行主机快照', default=list, blank=True)
    exec_mode = models.CharField('执行方式', max_length=20, blank=True)
    # 逐模块执行结果：[{order,name,task_type,status,return_code,duration,server_count,
    # attempts,skipped_over}]，skipped=被失败中止；attempts=执行次数（含重试）；
    # skipped_over=失败后被人工跳过（任务流继续但最终仍记失败）
    steps_result = models.JSONField('模块执行结果', default=list, blank=True)
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
