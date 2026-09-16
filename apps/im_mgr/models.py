"""飞书接入的配置面：应用凭据、回调入口、来源 IP 白名单与会话白名单。

本模块是 opsmanager 与飞书之间**唯一**的配置来源，由管理员在「系统配置 -
飞书接入」页维护。执行链路本身一行不改：飞书只是任务执行的又一个触发入口与
展示出口，最终都落到 `apps.task_exec.trigger`。

几处刻意的设计：

- **单例**：一个企业只会建一个自建应用，做成列表会让「到底哪条生效」变成
  运行时问题（而且是在回调验签失败时才暴露）。`FeishuApp` 强制 pk=1，取用
  一律走 `load()`。将来接钉钉/企微是另一张表，不是这张表的第二行。
- **回调路径带随机串**（`callback_slug`）：事件与卡片回调是本平台新增的两条
  公网入口，路径只配在飞书后台，等于一层共享密钥，扫描器打不到入口。轮换
  随机串 = 换回调地址，飞书后台必须同步改，所以给了独立动作而不是随便改。
- **来源 IP 白名单存库，不只写 nginx**：与 API 凭证同一套决策（后台 DB 为主
  + 防火墙兜底）。飞书出口 IP 段会变，改一处配置比改 nginx 再 reload 快；
  nginx 那层留作兜底，两层都在。与 `ApiCredential` 一样，**留空不等于不限制**，
  必须显式勾 `ip_unrestricted`——留空放行会让「忘了填」和「故意不填」外观一致。
- **凭据加密存储**：`app_secret` 拿到即可冒充机器人发消息，`encrypt_key` 拿到
  即可伪造回调事件，两者都必须可还原（要用原值调接口 / 解密回调），走
  `EncryptedTextField`（common/fields.py），只存哈希做不到。

启用开关是两级：`enabled` 管整个接入（回调、发卡片），`sso_enabled` 单独管
飞书登录。分开是因为前者出问题只是「飞书里用不了」，后者出问题是「登录信任链
被接管」——不该被同一个开关连带打开。
"""
import secrets

from django.db import models

from common.fields import EncryptedTextField
from common.request import ip_matches_rules

#: 回调随机串长度（token_hex 的字节数，落到路径上是两倍字符）
SLUG_BYTES = 16


def new_callback_slug():
    return secrets.token_hex(SLUG_BYTES)


class FeishuApp(models.Model):
    """飞书自建应用配置（单例，pk 恒为 1）。"""

    SINGLETON_PK = 1

    app_id = models.CharField('App ID', max_length=64, blank=True)
    # 三个秘密都要用原值：app_secret 换 tenant_access_token、encrypt_key 解密
    # 回调体并参与签名校验、verification_token 比对事件来源
    app_secret = EncryptedTextField('App Secret', blank=True)
    encrypt_key = EncryptedTextField('Encrypt Key', blank=True)
    verification_token = EncryptedTextField('Verification Token', blank=True)

    # 回调路径随机串：/api/feishu/<slug>/event/ 与 /api/feishu/<slug>/card/
    callback_slug = models.CharField('回调标识', max_length=64, default=new_callback_slug)

    ip_unrestricted = models.BooleanField('不限制来源 IP', default=False)
    ip_whitelist = models.TextField(
        '回调来源 IP 白名单', blank=True,
        help_text='飞书开放平台公布的回调出口 IP 或 CIDR，逗号/换行分隔',
    )

    enabled = models.BooleanField('启用飞书接入', default=False)
    sso_enabled = models.BooleanField('启用飞书登录', default=False)
    # 飞书账号在平台里找不到对应用户时是否自动建号。默认关：开了等于全公司飞书
    # 员工都能进后台，而「谁能进平台」不该由通讯录决定
    auto_create_user = models.BooleanField('未绑定用户时自动创建', default=False)

    remark = models.CharField('备注', max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = '飞书应用配置'
        verbose_name_plural = '飞书应用配置'
        db_table = 'im_feishu_app'

    def __str__(self):
        return f'FeishuApp({self.app_id or "未配置"})'

    def save(self, *args, **kwargs):
        """强制单例：任何保存都落到 pk=1。

        两处补偿，都是为了兜住「没走 load()、直接 new 一个实例保存」这条路：

        - `created_at` 是 auto_now_add，新实例上是 None，而 pk 已存在时 Django
          走的是 UPDATE，会把库里的创建时间抹成 NULL（sqlite 直接报 NOT NULL）；
        - `callback_slug` 有随机默认值，不回填就等于**静默轮换了回调地址**，
          表现是飞书那边突然全部 404，且没有任何操作记录指向它。
        """
        self.pk = self.SINGLETON_PK
        existing = (FeishuApp.objects.filter(pk=self.SINGLETON_PK)
                    .values('created_at', 'callback_slug').first())
        if existing is None:
            # 首次插入：update_fields 指向一条不存在的行，Django 会直接报错
            kwargs.pop('update_fields', None)
        elif self._state.adding:
            self.created_at = existing['created_at']
            self.callback_slug = existing['callback_slug']
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """禁止删除：配置行被删掉后回调会在「取不到配置」上失败，
        而表现是「飞书突然不工作了」，比留一条 enabled=False 的行难查得多。"""
        raise RuntimeError('飞书应用配置为单例，不支持删除；如需停用请置 enabled=False。')

    @classmethod
    def load(cls):
        """取配置行，不存在则建一条空的（带随机回调标识）。"""
        app, _ = cls.objects.get_or_create(
            pk=cls.SINGLETON_PK,
            defaults={'callback_slug': new_callback_slug()},
        )
        return app

    # ── 状态判定 ──────────────────────────────────────────
    @property
    def has_app_secret(self):
        return bool(self.app_secret)

    @property
    def has_encrypt_key(self):
        return bool(self.encrypt_key)

    @property
    def has_verification_token(self):
        return bool(self.verification_token)

    def missing_fields(self):
        """启用前缺失的必填项（中文名列表，空 = 齐全）。

        `encrypt_key` 与 `verification_token` 在飞书侧是可选的，这里当必填：
        方案要求回调强制验签，缺了就只能裸收——那条入口在公网上。
        """
        missing = []
        if not self.app_id:
            missing.append('App ID')
        if not self.has_app_secret:
            missing.append('App Secret')
        if not self.has_encrypt_key:
            missing.append('Encrypt Key')
        if not self.has_verification_token:
            missing.append('Verification Token')
        if not self.ip_unrestricted and not str(self.ip_whitelist or '').strip():
            missing.append('回调来源 IP 白名单（或显式勾选「不限制来源 IP」）')
        return missing

    def is_ready(self):
        """能否对外提供飞书能力（回调 / 发卡片）。"""
        return self.enabled and not self.missing_fields()

    def ip_allowed(self, ip):
        """回调来源 IP 是否放行。白名单留空且未显式勾选不限制 → 拒。

        与 ApiCredential.ip_allowed 同一口径；调用方取 IP 必须用
        common.request.auth_client_ip，不能用可伪造的 client_ip。
        """
        if self.ip_unrestricted:
            return True
        return ip_matches_rules(ip, self.ip_whitelist)

    # ── 回调地址 ──────────────────────────────────────────
    @property
    def event_path(self):
        return f'/api/feishu/{self.callback_slug}/event/'

    @property
    def card_path(self):
        return f'/api/feishu/{self.callback_slug}/card/'

    def rotate_callback_slug(self):
        """轮换回调标识，返回新值。旧地址立即失效，飞书后台必须同步改。"""
        self.callback_slug = new_callback_slug()
        self.save(update_fields=['callback_slug', 'updated_at'])
        return self.callback_slug


class FeishuChannel(models.Model):
    """允许与平台交互的飞书会话（群 / 私聊）白名单。

    群里的卡片谁都看得见，按钮谁都点得到——鉴权按点按钮的人判（见后续
    handlers），但「哪些会话能把执行入口摆出来」是另一道闸，必须显式登记。
    未登记的会话一律只读：收不到通知、也渲染不出按钮。

    `allow_execute` 与 `notify_enabled` 分开：多数群只需要收发布播报，不需要
    也不该能从群里直接推生产。
    """

    TYPE_CHOICES = [
        ('group', '群聊'),
        ('p2p', '单聊'),
    ]

    chat_id = models.CharField('会话 ID', max_length=128, unique=True)
    name = models.CharField('会话名称', max_length=100)
    chat_type = models.CharField('会话类型', max_length=20, choices=TYPE_CHOICES, default='group')
    allow_execute = models.BooleanField('允许发起任务执行', default=False)
    notify_enabled = models.BooleanField('接收通知播报', default=True)
    enabled = models.BooleanField('启用', default=True)
    remark = models.CharField('备注', max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = '飞书会话'
        verbose_name_plural = '飞书会话'
        db_table = 'im_feishu_channel'
        ordering = ['name']

    def __str__(self):
        return f'{self.name}({self.chat_id})'

    @classmethod
    def executable(cls, chat_id):
        """该会话当前是否允许发起执行（未登记 / 停用 / 未授权一律 False）。"""
        return cls.objects.filter(
            chat_id=chat_id, enabled=True, allow_execute=True).exists()
