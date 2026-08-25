from django.db import models


class Method(models.Model):
    """平台方法：可复用、可注册的执行能力。

    当前主力 provider 为 ansible_playbook —— 一个「方法」= 指定剧本 + 一组 tags，
    tags 由平台集中管理；其余 provider（云主机管理等）为后续扩展预留。
    provider 专属配置存放在 config（JSON）中，便于在不改表结构的前提下扩展。
    """

    PROVIDER_CHOICES = [
        ('ansible_playbook', 'Ansible 剧本'),
        ('jenkins', 'Jenkins 构建'),  # 触发 Jenkins job 构建（CI），连接信息在系统配置维护
        ('jumpserver', 'JumpServer 注册'),  # 目标主机注册为 JumpServer 资产，连接信息在系统配置维护
        ('cloud_host', '云主机管理'),  # 预留：对接云平台 API
    ]

    name = models.CharField('方法名称', max_length=150)
    code = models.SlugField('方法标识', max_length=100, unique=True)
    provider = models.CharField('执行 provider', max_length=30, choices=PROVIDER_CHOICES, default='ansible_playbook')
    description = models.CharField('说明', max_length=255, blank=True)
    # 部署类方法关联服务类型后，执行前会校验目标主机已完成该服务的规划（见 task_exec 校验）
    service_type = models.ForeignKey(
        'asset_mgr.ServiceType', verbose_name='关联服务类型', on_delete=models.PROTECT,
        null=True, blank=True, related_name='methods',
    )
    # provider 专属配置。ansible_playbook: {"playbook": "site.yml", "tags": "nginx,restart",
    #   "limit": ""}（主机/inventory 由任务执行入口按所选服务器动态生成）；
    #   jenkins: {"default_params": {"BRANCH": "master"}, "allow_unstable": false,
    #     "agent": "nat-gw-01"}（job 不绑定在方法上，由任务模块 params.job_name 指定；
    #     构建参数被模块 params 覆盖合并；agent 指定执行通道，留空按系统配置
    #     jenkins.agent 与通道路由解析，见 method_mgr/executors/jenkins.py）；
    #   cloud_host: {"action": "reboot", ...}
    config = models.JSONField('方法配置', default=dict, blank=True)
    # 预留：方法接受的参数定义，用于前端动态表单/校验
    params_schema = models.JSONField('参数定义', default=dict, blank=True)
    enabled = models.BooleanField('启用', default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = '平台方法'
        verbose_name_plural = '平台方法'
        db_table = 'platform_method'
        ordering = ['provider', 'name']

    def __str__(self):
        return f'{self.name} ({self.code})'
