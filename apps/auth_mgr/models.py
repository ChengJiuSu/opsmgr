import hmac
import secrets

from django.contrib.auth.models import AbstractUser
from django.conf import settings
from django.db import models
from django.utils import timezone

from common.crypto import decrypt, encrypt
from common.request import ip_matches_rules
from common.secrets import generate_secret, hash_secret


class Role(models.Model):
    """角色：模块权限矩阵 + 独立能力位。

    档位与模块清单见 apps/auth_mgr/roles.py，那里也写了「为什么不做成 CAM 那种
    策略语言」。矩阵放库里而不是写死在代码常量：「运维管理能不能改连接凭据」这类
    边界一定会反复调整，硬编码意味着每次调整都要发版。

    矩阵是**枚举**（模块 × 四档），配不出通配符、配不出「过宽」——这是它比策略
    语言安全的根本原因：写错最多是某个模块给多给少了一档，不会一行 `*` 把整个平台
    交出去。
    """

    name = models.CharField('角色名称', max_length=50, unique=True)
    code = models.CharField('角色标识', max_length=30, unique=True)
    # {模块: 权限档}，档位取值见 roles.LEVELS；缺失的模块视为 none
    permissions = models.JSONField('权限矩阵', default=dict)
    can_execute = models.BooleanField('可发起任务执行', default=False)
    can_exec_prod = models.BooleanField('可对生产环境执行', default=False)
    # 内置角色不可删（可改矩阵）：删掉「超级管理」会把所有人锁在门外
    is_builtin = models.BooleanField('内置角色', default=False)
    enabled = models.BooleanField('启用', default=True)
    remark = models.CharField('说明', max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = '角色'
        verbose_name_plural = '角色'
        db_table = 'auth_role'
        ordering = ['id']

    def __str__(self):
        return self.name

    def level_for(self, module):
        """该模块的权限档；未配置视为 none（fail-closed）。"""
        return (self.permissions or {}).get(module, 'none')


class User(AbstractUser):
    phone = models.CharField('手机号', max_length=20, blank=True)
    department = models.CharField('部门', max_length=100, blank=True)
    is_ldap = models.BooleanField('LDAP 用户', default=False)
    # 角色为空时回退 is_staff 语义（见 roles.user_level）：存量账号行为不变，
    # 不需要一次性给所有人配角色
    role = models.ForeignKey(
        Role, verbose_name='角色', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='users',
    )
    # ── 数据范围（空 = 不限）──
    # 与 API 凭证共用同一套语义（ApiCredential.allowed_*）。**只在三处生效**：
    # 主机列表收窄、任务执行发起校验、变量解析维度校验。其余列表不过滤——那是
    # 成本与漏检风险最大的一块，10 人无多租户不需要，字段留好将来要收紧再接。
    allowed_project_groups = models.ManyToManyField(
        'asset_mgr.ProjectGroup', verbose_name='可访问项目组', blank=True,
        related_name='allowed_users',
    )
    allowed_environments = models.ManyToManyField(
        'asset_mgr.Environment', verbose_name='可访问环境', blank=True,
        related_name='allowed_users',
    )

    class Meta:
        verbose_name = '用户'
        verbose_name_plural = '用户'
        db_table = 'auth_user'

    def __str__(self):
        return self.username

    # ── 数据范围（与 ApiCredential 同一套口径，实现见 common/dimscope.py）──
    def allowed_dims(self):
        if not hasattr(self, '_dim_cache'):
            groups = set(self.allowed_project_groups.values_list('id', flat=True))
            envs = set(self.allowed_environments.values_list('id', flat=True))
            self._dim_cache = (groups or None, envs or None)
        return self._dim_cache

    def allows_dims(self, project_group_id=None, environment_id=None):
        from common.dimscope import allows_dims

        return allows_dims(self.allowed_dims(), project_group_id, environment_id)

    def narrow(self, queryset, project_group_field='project_group_id',
               environment_field='environment_id'):
        from common.dimscope import narrow

        return narrow(queryset, self.allowed_dims(), project_group_field, environment_field)

    @property
    def scope_label(self):
        from common.dimscope import scope_label

        return scope_label(self.allowed_dims())


class UserMFA(models.Model):
    """用户的 TOTP 绑定。

    `enabled` 与记录存在不是一回事：`setup` 先落一条未启用记录（含密钥与恢复
    码），只有 `activate` 用一个正确验证码验过才置 True——否则用户扫错码就把
    自己锁在门外，只能找管理员重置。

    密钥经 Fernet 加密存储（common/crypto.py）：明文入库等于库泄露即可自造
    任何人的验证码，MFA 就白做了。
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, verbose_name='用户',
        on_delete=models.CASCADE, related_name='mfa',
    )
    secret_enc = models.TextField('密钥（加密）', editable=False)
    enabled = models.BooleanField('已启用', default=False)
    activated_at = models.DateTimeField('激活时间', null=True, blank=True)
    # 已用过的最大时间窗计数器：同一个 30 秒窗口的码用过就不再认，防重放
    last_counter = models.BigIntegerField('最近使用的时间窗', default=0)
    last_used_at = models.DateTimeField('最近校验通过时间', null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'MFA 绑定'
        verbose_name_plural = 'MFA 绑定'
        db_table = 'auth_user_mfa'

    def __str__(self):
        return f'{self.user_id} MFA({"on" if self.enabled else "off"})'

    def set_secret(self, raw):
        self.secret_enc = encrypt(raw)

    def get_secret(self):
        """返回明文密钥；解不开抛 common.crypto.DecryptError。"""
        return decrypt(self.secret_enc)


class MFARecoveryCode(models.Model):
    """一次性恢复码：验证器丢了的自救通道，只存哈希。"""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name='用户',
        on_delete=models.CASCADE, related_name='mfa_recovery_codes',
    )
    code_hash = models.CharField('恢复码哈希', max_length=128, editable=False)
    used_at = models.DateTimeField('使用时间', null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'MFA 恢复码'
        verbose_name_plural = 'MFA 恢复码'
        db_table = 'auth_mfa_recovery_code'
        ordering = ['id']
        indexes = [models.Index(fields=['user', 'used_at'])]

    def __str__(self):
        return f'{self.user_id} recovery({"used" if self.used_at else "unused"})'


class ApiCredential(models.Model):
    """机读接口凭证：内部脚本 / Webhook 免登录调用平台接口的身份。

    替代原来那把「全局唯一、明文存 SystemConfig、无过期无作用域无来源限制」
    的 `artifact.api_key`：一把 key 被所有脚本共用时，轮换要同时改所有调用方，
    泄露了也无法定位是哪个脚本漏的。

    四道门叠加，任一不过即拒（校验实现见 apps/auth_mgr/apikey.py）：

    1. **key** —— 明文形如 `ak_<16位hex>.<43位随机串>`。前半段 key_id 公开、
       建索引用于定位记录；后半段 secret 只存 sha256（common/secrets.py），
       明文仅在创建/轮换的响应里出现一次。
    2. **IP 白名单** —— 按凭证维护 CIDR 列表。客户端 IP 走
       common.request.auth_client_ip（可信代理链解析），不吃伪造的 XFF。
    3. **作用域 scopes** —— 一把 key 只能调被授权的那几类接口，泄露面按接口切分。
    4. **Webhook 签名** —— 第三方 webhook 的源 IP 段大且会变，只靠白名单不够；
       配了 sig_mode 就强制验签，且校验发生在认证层——放权限层容易在新增视图
       时漏挂，认证层是所有走这条认证的接口都必过。

    `ip_whitelist` 为空**不等于**不限制：必须显式把 `ip_unrestricted` 置 True，
    否则认证直接拒。默认拒的理由：留空放行会让「忘了填」和「故意不填」外观
    一致，一次手滑就产出一把全网可用的 key。
    """

    SIG_NONE = 'none'
    SIG_HMAC_SHA256 = 'hmac_sha256'
    SIG_TOKEN = 'token'
    SIG_MODE_CHOICES = [
        (SIG_NONE, '不验签'),
        (SIG_HMAC_SHA256, 'HMAC-SHA256（GitHub/Gitee 风格，签请求体）'),
        (SIG_TOKEN, '共享 Token（GitLab 风格，比对请求头明文）'),
    ]

    KEY_PREFIX = 'ak_'
    #: 明文里分隔 key_id 与 secret 的字符（secret 用 token_urlsafe，不含 '.'）
    KEY_SEP = '.'

    name = models.CharField('凭证名称', max_length=64, unique=True)
    key_id = models.CharField('凭证标识', max_length=32, unique=True, editable=False)
    secret_hash = models.CharField('密钥哈希', max_length=64, editable=False)
    # 授权作用域，取值见 apps/auth_mgr/apikey.SCOPES
    scopes = models.JSONField('作用域', default=list)

    ip_unrestricted = models.BooleanField('不限制来源 IP', default=False)
    ip_whitelist = models.TextField('来源 IP 白名单', blank=True,
                                    help_text='IP 或 CIDR，逗号/换行分隔')

    sig_mode = models.CharField('签名校验方式', max_length=16,
                                choices=SIG_MODE_CHOICES, default=SIG_NONE)
    sig_header = models.CharField('签名请求头', max_length=64, blank=True)
    sig_prefix = models.CharField('签名值前缀', max_length=16, blank=True,
                                  help_text='如 GitHub 的 sha256=；留空表示纯 hex')
    # 需要用原值重算 HMAC，不能只存哈希，故走 Fernet 加密（common/crypto.py）
    signing_secret_enc = models.TextField('签名密钥（加密）', blank=True, editable=False)

    rate_limit = models.CharField('限速', max_length=16, blank=True,
                                  help_text='DRF 速率语法，如 60/min；留空用全局默认')

    # ── 数据范围（空 = 不限；纯收窄，配不出比作用域更宽的权限）──
    # 作用域管「能调哪类接口」，这两个管「能作用在哪些资源上」，两层正交。
    # 一把部署机上的 key 不该能读到全公司的主机清单与变量。
    allowed_project_groups = models.ManyToManyField(
        'asset_mgr.ProjectGroup', verbose_name='可访问项目组', blank=True,
        related_name='api_credentials',
    )
    allowed_environments = models.ManyToManyField(
        'asset_mgr.Environment', verbose_name='可访问环境', blank=True,
        related_name='api_credentials',
    )

    # 凭证的行权身份：审计表按 user 记录调用方，权限类也照常生效。
    # PROTECT：删用户前必须先处理引用它的凭证，避免凭证悄悄失去身份
    bound_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name='服务账号',
        on_delete=models.PROTECT, related_name='api_credentials',
    )
    enabled = models.BooleanField('启用', default=True)
    expires_at = models.DateTimeField('过期时间', null=True, blank=True)

    last_used_at = models.DateTimeField('最近使用', null=True, blank=True)
    last_used_ip = models.CharField('最近来源 IP', max_length=64, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name='创建人', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='created_api_credentials',
    )
    remark = models.CharField('备注', max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'API 凭证'
        verbose_name_plural = 'API 凭证'
        db_table = 'auth_api_credential'
        ordering = ['name']

    def __str__(self):
        return f'{self.name}({self.key_id})'

    # ── 明文与校验 ────────────────────────────────────────
    @classmethod
    def generate_key(cls):
        """生成一把新 key，返回 (明文, key_id, secret 哈希)。

        明文只在创建/轮换响应里出现一次，库里永远只有哈希。
        """
        key_id = cls.KEY_PREFIX + secrets.token_hex(8)
        secret = generate_secret()
        return f'{key_id}{cls.KEY_SEP}{secret}', key_id, hash_secret(secret)

    @classmethod
    def split_key(cls, raw):
        """明文 → (key_id, secret)；不是本系统的 key 格式返回 (None, None)。

        返回 None 让认证类原样放行给下一个认证方式/旧 key 兜底，而不是报错——
        废弃期内老的 `artifact.api_key` 还在同一个请求头里传。
        """
        raw = str(raw or '').strip()
        if not raw.startswith(cls.KEY_PREFIX) or cls.KEY_SEP not in raw:
            return None, None
        key_id, _, secret = raw.partition(cls.KEY_SEP)
        return (key_id, secret) if secret else (None, None)

    def verify_secret(self, secret):
        """恒定时间比对 secret 哈希。"""
        if not secret or not self.secret_hash:
            return False
        return hmac.compare_digest(hash_secret(secret), self.secret_hash)

    def rotate_key(self):
        """轮换：旧明文立即失效，返回新明文（仅此一次可见）。"""
        raw, key_id, secret_hash = self.generate_key()
        self.key_id = key_id
        self.secret_hash = secret_hash
        self.save(update_fields=['key_id', 'secret_hash', 'updated_at'])
        return raw

    # ── 状态与授权 ────────────────────────────────────────
    def is_expired(self):
        return bool(self.expires_at and self.expires_at <= timezone.now())

    def is_usable(self):
        return bool(self.enabled and not self.is_expired())

    def has_scope(self, scope):
        return scope in (self.scopes or [])

    def ip_allowed(self, ip):
        """来源 IP 是否放行。白名单留空且未显式勾选不限制 → 拒。"""
        if self.ip_unrestricted:
            return True
        return ip_matches_rules(ip, self.ip_whitelist)

    # ── 数据范围 ──────────────────────────────────────────
    def allowed_dims(self):
        """缓存一次范围 id 集合：一个请求里可能反复判定，不该每次查表。

        返回 (项目组 id 集合 或 None, 环境 id 集合 或 None)，None = 不限。
        """
        if not hasattr(self, '_dim_cache'):
            groups = set(self.allowed_project_groups.values_list('id', flat=True))
            envs = set(self.allowed_environments.values_list('id', flat=True))
            self._dim_cache = (groups or None, envs or None)
        return self._dim_cache

    def allows_dims(self, project_group_id=None, environment_id=None):
        """该维度组合是否在凭证范围内（实现与登录用户共用 common/dimscope）。"""
        from common.dimscope import allows_dims

        return allows_dims(self.allowed_dims(), project_group_id, environment_id)

    def narrow(self, queryset, project_group_field='project_group_id',
               environment_field='environment_id'):
        """按凭证范围收窄查询集（列表接口用）。"""
        from common.dimscope import narrow

        return narrow(queryset, self.allowed_dims(), project_group_field, environment_field)

    @property
    def scope_label(self):
        from common.dimscope import scope_label

        return scope_label(self.allowed_dims())

    # ── Webhook 签名密钥 ──────────────────────────────────
    def set_signing_secret(self, raw):
        self.signing_secret_enc = encrypt(raw)

    def get_signing_secret(self):
        """返回明文签名密钥；解不开抛 common.crypto.DecryptError。"""
        return decrypt(self.signing_secret_enc)

    @property
    def has_signing_secret(self):
        return bool(self.signing_secret_enc)

    # ── 使用痕迹 ──────────────────────────────────────────
    def touch(self, ip):
        """更新最近使用信息（调用方按 API_KEY_TOUCH_INTERVAL 节流）。"""
        self.last_used_at = timezone.now()
        self.last_used_ip = (ip or '')[:64]
        # update_fields 限定三列：并发写不同凭证互不影响，也不会覆盖页面上的改动
        ApiCredential.objects.filter(pk=self.pk).update(
            last_used_at=self.last_used_at,
            last_used_ip=self.last_used_ip,
            updated_at=timezone.now(),  # .update() 不触发 auto_now，手动补
        )
