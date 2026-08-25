from django.contrib.auth.models import AbstractUser
from django.conf import settings
from django.db import models

from common.crypto import decrypt, encrypt


class User(AbstractUser):
    phone = models.CharField('手机号', max_length=20, blank=True)
    department = models.CharField('部门', max_length=100, blank=True)
    is_ldap = models.BooleanField('LDAP 用户', default=False)

    class Meta:
        verbose_name = '用户'
        verbose_name_plural = '用户'
        db_table = 'auth_user'

    def __str__(self):
        return self.username


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
