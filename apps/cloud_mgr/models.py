"""云控管理：云账号、域名/解析记录、证书及分发。

第一期落地域名管理（云账号 + 主域名 + 子域名解析），证书模型先建表，
申请/续签/分发链路在 P2/P3 实现。
"""
from django.conf import settings
from django.db import models

from common.fields import EncryptedTextField

from apps.asset_mgr.models import CloudProvider, Server


class CloudAccount(models.Model):
    """云账号：云厂商 API 凭据（AK/SK）。DNS 解析、证书 DNS 验证及后续云资源模块共用。

    secret_key 只写不回显（序列化器层控制），沿用 Credential 模式，并用
    EncryptedTextField 加密存储——它是云账号的完整控制权，明文入库等于库泄露
    即云资源被接管。
    """

    name = models.CharField('账号名称', max_length=100, unique=True)
    provider = models.ForeignKey(
        CloudProvider, verbose_name='云厂商', on_delete=models.PROTECT,
        related_name='cloud_accounts',
    )
    access_key = models.CharField('AccessKey ID', max_length=128)
    secret_key = EncryptedTextField('AccessKey Secret')
    enabled = models.BooleanField('是否启用', default=True)
    remark = models.TextField('备注', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = '云账号'
        verbose_name_plural = '云账号'
        db_table = 'cloud_account'
        ordering = ['name']

    def __str__(self):
        return self.name


class Domain(models.Model):
    """主域名：归属某个云账号（该账号所在厂商的 DNS 服务托管解析）。"""

    STATUS_CHOICES = [
        ('normal', '正常'),
        ('abnormal', '异常'),
    ]

    name = models.CharField('域名', max_length=253, unique=True)
    cloud_account = models.ForeignKey(
        CloudAccount, verbose_name='云账号', on_delete=models.PROTECT,
        related_name='domains',
    )
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='normal')
    remark = models.TextField('备注', blank=True)
    last_synced_at = models.DateTimeField('最近同步时间', null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = '主域名'
        verbose_name_plural = '主域名'
        db_table = 'cloud_domain'
        ordering = ['name']

    def __str__(self):
        return self.name


class DnsRecord(models.Model):
    """子域名解析记录。

    绑定目标：target_type 标记指向的资源类型；server 仅 target_type=server 时使用，
    选定后 value 取主机 IP。LB/GA/CDN 第一期手工填 value，类型字段为后续
    对接对应资源模块预留。

    同步说明：remote_record_id 为厂商侧记录 ID；acme.sh DNS 验证产生的
    `_acme-challenge` TXT 记录由验证流程自动增删，从厂商侧同步时须忽略，
    避免验证窗口期被误采集入库。
    """

    TYPE_CHOICES = [
        ('A', 'A'),
        ('AAAA', 'AAAA'),
        ('CNAME', 'CNAME'),
        ('TXT', 'TXT'),
        ('MX', 'MX'),
        ('NS', 'NS'),
        ('SRV', 'SRV'),
        ('CAA', 'CAA'),
    ]

    TARGET_CHOICES = [
        ('none', '无绑定'),
        ('server', '服务器'),
        ('lb', '负载均衡'),
        ('ga', '全球加速'),
        ('cdn', 'CDN'),
        ('custom', '自定义'),
    ]

    SYNC_STATUS_CHOICES = [
        ('pending', '待推送'),
        ('synced', '已同步'),
        ('failed', '推送失败'),
    ]

    domain = models.ForeignKey(
        Domain, verbose_name='主域名', on_delete=models.CASCADE,
        related_name='records',
    )
    # 长度受 uniq_dns_record 联合唯一索引限制：InnoDB 单索引上限 3072 字节，
    # utf8mb4 下 domain(8)+rr(512)+type(40)+value(2048)+line(200)=2808，不可再放宽
    rr = models.CharField('主机记录', max_length=128, help_text='如 www、@、*')
    type = models.CharField('记录类型', max_length=10, choices=TYPE_CHOICES, default='A')
    value = models.CharField('记录值', max_length=512)
    ttl = models.PositiveIntegerField('TTL(秒)', default=600)
    line = models.CharField('解析线路', max_length=50, blank=True, default='default')
    target_type = models.CharField('绑定类型', max_length=20, choices=TARGET_CHOICES, default='none')
    server = models.ForeignKey(
        Server, verbose_name='绑定服务器', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='dns_records',
    )
    remote_record_id = models.CharField('厂商记录ID', max_length=64, blank=True)
    sync_status = models.CharField('同步状态', max_length=20, choices=SYNC_STATUS_CHOICES, default='pending')
    sync_error = models.TextField('同步错误', blank=True)
    enabled = models.BooleanField('是否启用', default=True)
    remark = models.TextField('备注', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = '解析记录'
        verbose_name_plural = '解析记录'
        db_table = 'cloud_dns_record'
        ordering = ['rr', 'id']
        constraints = [
            # 同域名下 主机记录+类型+值+线路 唯一，防重复解析
            models.UniqueConstraint(
                fields=['domain', 'rr', 'type', 'value', 'line'],
                name='uniq_dns_record',
            ),
        ]

    def __str__(self):
        return f'{self.rr}.{self.domain.name} {self.type}'

    @property
    def fqdn(self):
        return self.domain.name if self.rr == '@' else f'{self.rr}.{self.domain.name}'


class Certificate(models.Model):
    """域名证书：acme.sh DNS 验证签发（P2 实现申请/续签链路）。

    private_key_pem 只写不回显。domains 为 SAN 列表（JSON），首项为主域名，
    支持通配符（*.example.com）。
    """

    STATUS_CHOICES = [
        ('pending', '待申请'),
        ('issuing', '申请中'),
        ('issued', '已签发'),
        ('failed', '申请失败'),
        ('expired', '已过期'),
    ]

    name = models.CharField('证书名称', max_length=100, unique=True)
    domains = models.JSONField('域名列表', default=list)
    cloud_account = models.ForeignKey(
        CloudAccount, verbose_name='DNS验证账号', on_delete=models.PROTECT,
        related_name='certificates',
    )
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    cert_pem = models.TextField('证书(PEM)', blank=True)
    private_key_pem = EncryptedTextField('私钥(PEM)', blank=True)
    chain_pem = models.TextField('证书链(PEM)', blank=True)
    issued_at = models.DateTimeField('签发时间', null=True, blank=True)
    expires_at = models.DateTimeField('过期时间', null=True, blank=True)
    auto_renew = models.BooleanField('自动续签', default=True)
    last_error = models.TextField('最近错误', blank=True)
    remark = models.TextField('备注', blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name='创建人', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = '证书'
        verbose_name_plural = '证书'
        db_table = 'cloud_certificate'
        ordering = ['-id']

    def __str__(self):
        return self.name


class CertDeployment(models.Model):
    """证书分发目标。

    - server：经 ansible 推送 crt/key/fullchain 到 deploy_path 并执行 reload_cmd；
      文件名与下载包一致（<证书名>.crt/.key/.fullchain.pem）；
    - lb/ga/cdn：接口预留（deploy.py 中各自 deployer 桩），资源模块接入后实现；
    - auto_deploy：证书签发/续签成功后自动重新分发（续签后无需人工推送）。
    """

    TARGET_CHOICES = [
        ('server', '服务器'),
        ('lb', '负载均衡'),
        ('ga', '全球加速'),
        ('cdn', 'CDN'),
    ]

    STATUS_CHOICES = [
        ('pending', '待部署'),
        ('deploying', '部署中'),
        ('success', '成功'),
        ('failed', '失败'),
    ]

    certificate = models.ForeignKey(
        Certificate, verbose_name='证书', on_delete=models.CASCADE,
        related_name='deployments',
    )
    target_type = models.CharField('目标类型', max_length=20, choices=TARGET_CHOICES, default='server')
    server = models.ForeignKey(
        Server, verbose_name='目标服务器', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='cert_deployments',
    )
    credential = models.ForeignKey(
        'task_exec.Credential', verbose_name='连接凭据', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='cert_deployments',
    )
    deploy_path = models.CharField('部署目录', max_length=255, default='/etc/nginx/ssl')
    reload_cmd = models.CharField('重载命令', max_length=255, blank=True)
    auto_deploy = models.BooleanField('签发后自动分发', default=True)
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    last_deployed_at = models.DateTimeField('最近部署时间', null=True, blank=True)
    last_error = models.TextField('最近错误', blank=True)
    last_output = models.TextField('最近执行输出', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = '证书分发'
        verbose_name_plural = '证书分发'
        db_table = 'cloud_cert_deployment'
        ordering = ['-id']
        constraints = [
            # 同证书对同一目标（同路径）不重复登记
            models.UniqueConstraint(
                fields=['certificate', 'target_type', 'server', 'deploy_path'],
                name='uniq_cert_deployment_target',
            ),
        ]

    def __str__(self):
        return f'{self.certificate.name} -> {self.get_target_type_display()}'
