"""云控管理：云账号、域名/解析记录、证书及分发。

第一期落地域名管理（云账号 + 主域名 + 子域名解析），证书模型先建表，
申请/续签/分发链路在 P2/P3 实现。
"""
from django.conf import settings
from django.db import models
from django.utils import timezone

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
    # 受保护域名：其下所有解析变更一律按高危处理（走审批），用于生产主域名
    protected = models.BooleanField('受保护域名', default=False)
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
    # 受保护记录：改/删按高危处理（走审批）。同步与新建时自动给 @/NS/MX 打上，
    # 也可人工标记（见 dns_risk.auto_protected）
    protected = models.BooleanField('受保护记录', default=False)
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


class DnsChangeLog(models.Model):
    """解析记录变更留痕：改/删**之前**的云端真实值 + 一键回滚依据。

    为什么必须有：原来删记录是「先删云端，再 record.delete()」——删完本地行没了，
    而 audit 表里 DELETE 请求没有 body，等于「被删的是什么值」在系统里查不到，
    只能去云控台翻厂商操作日志。改也一样：audit 有新值没有旧值。

    审批防的是「改错」，这张表防的是「改错之后」——而且回滚不需要任何人在线。

    前值一律**从厂商侧拉**而不是用本地值：`sync_status=failed` 的记录本地与云端
    本就不一致，拉云端才是真实前值。拉不到时退回本地值并在 `before_source` 里
    标注，否则回滚时分不清这份快照可不可信。

    域名/记录用 SET_NULL 弱关联并同时存名称快照：记录删了、域名删了，这条历史
    仍要能回答「删掉的是哪条」。
    """

    ACTION_CHOICES = [
        ('create', '新增'),
        ('update', '修改'),
        ('delete', '删除'),
        ('restore', '回滚'),
    ]
    SOURCE_CHOICES = [
        ('provider', '云厂商实时值'),
        ('local', '本地值（厂商拉取失败）'),
        ('missing', '云端无此记录'),
        ('none', '无前值（新增）'),
    ]

    domain = models.ForeignKey(
        Domain, verbose_name='主域名', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='change_logs',
    )
    domain_name = models.CharField('主域名快照', max_length=253)
    record = models.ForeignKey(
        DnsRecord, verbose_name='解析记录', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='change_logs',
    )
    # 记录标识快照：record 被删/被 SET_NULL 后靠这几个字段辨识
    record_rr = models.CharField('主机记录', max_length=128, blank=True)
    record_type = models.CharField('记录类型', max_length=10, blank=True)
    remote_record_id = models.CharField('厂商记录ID', max_length=64, blank=True)

    action = models.CharField('变更动作', max_length=16, choices=ACTION_CHOICES)
    # {rr,type,value,ttl,line,enabled,remote_record_id}；新增时为空
    before = models.JSONField('变更前值', default=dict, blank=True)
    before_source = models.CharField('前值来源', max_length=16,
                                     choices=SOURCE_CHOICES, default='none')
    after = models.JSONField('变更后值', default=dict, blank=True)
    reason = models.CharField('变更原因', max_length=200, blank=True)
    operator = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name='操作人', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='dns_change_logs',
    )
    restored_at = models.DateTimeField('回滚时间', null=True, blank=True)
    restored_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name='回滚人', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='dns_restores',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'DNS 变更留痕'
        verbose_name_plural = 'DNS 变更留痕'
        db_table = 'cloud_dns_change_log'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['domain', '-created_at']),
            models.Index(fields=['record', '-created_at']),
        ]

    def __str__(self):
        return f'{self.get_action_display()} {self.record_rr}.{self.domain_name}'

    @property
    def restorable(self):
        """能否回滚。

        - 新增：回滚 = 删掉这条记录，需要记录还在；
        - 改/删：回滚 = 把前值写回去，需要有可信前值（云端拉到过）；
        - 回滚本身不再回滚（要撤销回滚就回滚它对应的那条新留痕）。
        """
        if self.restored_at or self.action == 'restore':
            return False
        if self.action == 'create':
            return self.record_id is not None
        return bool(self.before) and self.before_source == 'provider'


class DnsChangeRequest(models.Model):
    """解析记录的高危变更单（申请 → 审批 → 系统执行）。

    只对**高危**变更强制（判定见 dns_risk.py）：加一条新的测试子域名不必审批，
    改根域 A 记录或删 MX 才要。全都拦会让人把审批当障碍去想办法躲，全都不拦
    等于没有防线。

    三个决定成败的细节，都在这里落死：

    - **申请人不能自批**（`emergency=False` 时）。10 人团队里最容易被绕过的
      就是这一条；
    - **必须有紧急通道**（`emergency=True`：自批 + 强制填原因 + 审计高亮 + 日志
      warning）。没有它，半夜故障没人审批，最后的结果是大家互相开超级管理账号
      ——审批流反而降低了安全性；
    - **过期作废**（settings.DNS_APPROVAL_TTL_HOURS，默认 24 小时）。陈旧变更单
      被误批推上去是这类系统的典型事故。

    再加一道：**执行前重新拉云端值比对**。申请时的前值与执行时的现值不一致
    （期间有人改过）就拒绝执行，要求重新提交——这正是「陈旧变更单」真正危险的
    地方，光靠过期时间挡不住 24 小时内的漂移。
    """

    STATUS_PENDING = 'pending'
    STATUS_APPLIED = 'applied'
    STATUS_REJECTED = 'rejected'
    STATUS_CANCELLED = 'cancelled'
    STATUS_EXPIRED = 'expired'
    STATUS_CHOICES = [
        (STATUS_PENDING, '待审批'),
        (STATUS_APPLIED, '已执行'),
        (STATUS_REJECTED, '已驳回'),
        (STATUS_CANCELLED, '已撤销'),
        (STATUS_EXPIRED, '已过期'),
    ]
    ACTION_CHOICES = [
        ('create', '新增'),
        ('update', '修改'),
        ('delete', '删除'),
    ]

    domain = models.ForeignKey(
        Domain, verbose_name='主域名', on_delete=models.CASCADE,
        related_name='change_requests',
    )
    # 改/删指向现有记录；新增时为空。记录被别处删掉后变更单自动失去目标（执行时报错）
    record = models.ForeignKey(
        DnsRecord, verbose_name='解析记录', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='change_requests',
    )
    action = models.CharField('变更动作', max_length=16, choices=ACTION_CHOICES)
    # 目标值：{rr,type,value,ttl,line,enabled,target_type,server_id}；删除时为空
    payload = models.JSONField('目标值', default=dict, blank=True)
    # 申请时的云端现值（供审批人看 diff，也是执行前漂移比对的基准）
    before = models.JSONField('申请时线上值', default=dict, blank=True)
    before_source = models.CharField('前值来源', max_length=16, default='none')
    # 命中的高危理由（dns_risk.risk_reasons），审批人据此判断
    risk_reasons = models.JSONField('高危理由', default=list, blank=True)

    reason = models.CharField('申请原因', max_length=200)
    status = models.CharField('状态', max_length=16, choices=STATUS_CHOICES,
                              default=STATUS_PENDING)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name='申请人', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='dns_change_requests',
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name='审批人', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='dns_change_reviews',
    )
    reviewed_at = models.DateTimeField('审批时间', null=True, blank=True)
    review_comment = models.CharField('审批意见', max_length=200, blank=True)
    # 紧急自批：申请人自己批准。审计与列表里高亮，事后必查
    emergency = models.BooleanField('紧急自批', default=False)
    expires_at = models.DateTimeField('失效时间')
    # 执行后关联的变更留痕（回滚入口在那边）
    change_log = models.ForeignKey(
        'DnsChangeLog', verbose_name='变更留痕', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'DNS 变更单'
        verbose_name_plural = 'DNS 变更单'
        db_table = 'cloud_dns_change_request'
        ordering = ['-created_at']
        indexes = [models.Index(fields=['status', '-created_at'])]

    def __str__(self):
        rr = self.payload.get('rr') or getattr(self.record, 'rr', '?')
        return f'{self.get_action_display()} {rr}.{self.domain.name}（{self.get_status_display()}）'

    @property
    def expired(self):
        return self.status == self.STATUS_PENDING and self.expires_at <= timezone.now()

    @property
    def pending(self):
        return self.status == self.STATUS_PENDING and not self.expired

    @property
    def target_rr(self):
        return self.payload.get('rr') or getattr(self.record, 'rr', '')

    @property
    def target_type_label(self):
        return self.payload.get('type') or getattr(self.record, 'type', '')
