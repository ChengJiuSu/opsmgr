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
from datetime import timedelta

from django.conf import settings
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
    # 授权回调地址（浏览器跳回的前端页），必须与飞书后台「重定向 URL」白名单
    # 一致。留空则按请求域名推导出 <scheme>://<host>/login/feishu
    sso_redirect_uri = models.CharField('飞书登录回调地址', max_length=500, blank=True)
    # 允许在「与机器人的单聊」里发起执行（机器人自定义菜单、私聊 @机器人）。
    # 默认关：会话白名单管的是「哪些会话能摆出执行入口」，单聊没有 chat_id 可登记
    # （菜单事件里压根没有），只能用一个显式总开关表达同一个意思。
    # 单聊天然一对一、只有绑定用户本人可见，群里那种「谁都能点」的风险不存在，
    # 但「只许从运维群发起、不许私聊发起」是合理诉求，所以仍要管理员点头
    allow_p2p_execute = models.BooleanField('允许单聊发起执行', default=False)

    REVIEW_CHOICES = [
        ('off', '关闭'),
        ('prod', '仅生产环境'),
        ('all', '所有任务'),
    ]
    # 双人复核：发起人确认之后，还要**另一个**有生产执行权限的人点一次才真的跑。
    # 默认关——它拦的是「一个人在错误的时间对着错误的环境点了确认」，收益真实但
    # 代价也真实（多一个人在线才能发版），开不开是团队自己的取舍。
    # **只在群会话里可用**：单聊里那张卡只有发起人看得见，没有第二个人能点。
    dual_review = models.CharField('双人复核', max_length=20,
                                   choices=REVIEW_CHOICES, default='off')

    SUDO_CHOICES = [
        ('off', '关闭'),
        ('fallback', '仅未绑定验证器的用户'),
        ('all', '所有人'),
    ]
    # 敏感操作二次验证（sudo）可否用飞书卡片确认代替验证码。
    # 默认 fallback：飞书点一下比翻验证器摩擦小，容易养成「随手确认」的习惯，
    # 而 MFA fatigue 正是靠这个得手的。绑了验证器的人用验证器，没有其他手段的
    # （飞书自动建号、没有可用密码）才走卡片——那类账号否则连配置页都改不了
    sudo_confirm = models.CharField('sudo 卡片确认', max_length=20,
                                    choices=SUDO_CHOICES, default='fallback')
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
    #: 回调入口的公共前缀。与配置面路径分开（不是 /api/feishu/<slug>/…），
    #: 这样审计中间件能整段跳过回调流量而不影响配置接口的审计——群里每条消息
    #: 都是一次事件回调，落进审计表只有噪声（匿名 + 一坨密文），没有价值
    CALLBACK_PREFIX = '/api/feishu/callback/'

    @property
    def event_path(self):
        return f'{self.CALLBACK_PREFIX}{self.callback_slug}/event/'

    @property
    def card_path(self):
        return f'{self.CALLBACK_PREFIX}{self.callback_slug}/card/'

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

    SCOPE_CHOICES = [
        ('failed', '仅失败'),
        ('all', '全部结果'),
    ]

    # 群用 chat_id（oc_…），也接受个人 open_id（ou_…）——只用单聊的人同样需要
    # 收播报，而单聊的 chat_id 飞书界面上和 im/v1/chats 都拿不到
    chat_id = models.CharField('会话 ID / 个人 OpenID', max_length=128, unique=True)
    name = models.CharField('会话名称', max_length=100)
    chat_type = models.CharField('会话类型', max_length=20, choices=TYPE_CHOICES, default='group')
    allow_execute = models.BooleanField('允许发起任务执行', default=False)
    notify_enabled = models.BooleanField('接收通知播报', default=True)
    # 播报范围。默认只播失败：成功的执行每天几十上百次，全播等于把群变成噪声源，
    # 而噪声一多，真正该被看见的失败就被划过去了
    notify_scope = models.CharField('播报范围', max_length=20,
                                    choices=SCOPE_CHOICES, default='failed')
    # 订阅维度（空 = 不限）。与平台其余地方同一套「维度」口径，不引入新概念：
    # 一个群只关心自己那摊事，别的项目组的失败播过来只会被当噪声
    notify_project_group = models.ForeignKey(
        'asset_mgr.ProjectGroup', verbose_name='仅播报该项目组', on_delete=models.PROTECT,
        null=True, blank=True, related_name='feishu_channels',
    )
    notify_environment = models.ForeignKey(
        'asset_mgr.Environment', verbose_name='仅播报该环境', on_delete=models.PROTECT,
        null=True, blank=True, related_name='feishu_channels',
    )
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

    @property
    def receive_id_type(self):
        """发消息时的收件人类型。飞书的 open_id 固定 `ou_` 前缀，群是 `oc_`。"""
        return 'open_id' if str(self.chat_id).startswith('ou_') else 'chat_id'

    @classmethod
    def broadcast_targets(cls, task, status):
        """该播报给哪些会话。

        三道过滤：开着播报开关、范围涵盖这次结果（失败一律播，成功只播给
        `notify_scope='all'` 的）、订阅维度与任务维度相容（空 = 不限）。
        """
        qs = cls.objects.filter(enabled=True, notify_enabled=True)
        if status != 'failed':
            qs = qs.filter(notify_scope='all')
        return [
            row for row in qs
            if (row.notify_project_group_id in (None, task.project_group_id)
                and row.notify_environment_id in (None, task.environment_id))
        ]

    @classmethod
    def executable(cls, chat_id):
        """该会话当前是否允许发起执行（未登记 / 停用 / 未授权一律 False）。"""
        return cls.objects.filter(
            chat_id=chat_id, enabled=True, allow_execute=True).exists()


class FeishuBinding(models.Model):
    """平台用户与飞书身份的绑定。

    飞书回调只给 `open_id`，不映射到平台 User 的话，RBAC、can_exec_prod、数据
    范围与审计**全部失效**——这是整个飞书接入的安全底座，不能省。

    **以 union_id 为准**：`open_id` 是「同一个人在某个应用下」的标识，换一个
    自建应用就变；`union_id` 在同一开发者账号下稳定。open_id 一并存下来是因为
    回调里只有它，要靠它反查。

    绑定来源（`source`）留痕：出问题时要能分清「管理员手工绑的」与「按邮箱自动
    匹配上的」——后者依赖通讯录邮箱与平台账号邮箱一致，是最容易绑错人的一条路。
    """

    SOURCE_CHOICES = [
        ('manual', '管理员绑定'),
        ('email', '邮箱自动匹配'),
        ('auto_create', '自动创建账号'),
    ]

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, verbose_name='平台用户',
        on_delete=models.CASCADE, related_name='feishu_binding',
    )
    # 一个平台账号只绑一个飞书身份（OneToOne），反过来也唯一
    union_id = models.CharField('飞书 UnionID', max_length=128, unique=True)
    open_id = models.CharField('飞书 OpenID', max_length=128, blank=True, db_index=True)
    feishu_name = models.CharField('飞书昵称', max_length=100, blank=True)
    feishu_email = models.CharField('飞书邮箱', max_length=254, blank=True)
    source = models.CharField('绑定来源', max_length=20, choices=SOURCE_CHOICES, default='manual')
    last_login_at = models.DateTimeField('最近飞书登录', null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = '飞书绑定'
        verbose_name_plural = '飞书绑定'
        db_table = 'im_feishu_binding'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.user_id}↔{self.union_id}'

    @classmethod
    def resolve(cls, union_id=None, open_id=None):
        """按 union_id 优先、open_id 兜底查绑定。

        open_id 兜底是给卡片回调用的：那里拿不到 union_id。
        """
        query = cls.objects.select_related('user')
        if union_id:
            hit = query.filter(union_id=union_id).first()
            if hit:
                return hit
        if open_id:
            return query.filter(open_id=open_id).first()
        return None


class FeishuConfirmToken(models.Model):
    """生产执行的一次性确认票据。

    机器触发没有「弹窗再确认」那一步，所以确认被前移到第二张卡片上。票据把三件
    事钉死，缺一件这道确认就形同虚设：

    - **一次性**：`used_at` 条件更新消费，并发双击只有一次生效；
    - **限本人**：`open_id` 必须与点按钮的人一致——群里的卡片谁都看得见，
      不限人的话「A 发起、B 确认」就绕过了 A 的权限判定；
    - **短有效期**：过期作废，防止卡片被翻旧账再点一次。

    票据只是「这次确认有效」的凭据，**不携带任何执行参数**：任务、主机范围都在
    消费时按库里的当前值重新解析，卡片上的值只用于展示。
    """

    token = models.CharField('票据', max_length=64, unique=True)
    task = models.ForeignKey(
        'task_exec.ExecutionTask', verbose_name='任务', on_delete=models.CASCADE,
        related_name='feishu_confirm_tokens',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name='发起人', on_delete=models.CASCADE,
        related_name='feishu_confirm_tokens',
    )
    STAGE_CHOICES = [
        ('confirm', '发起人确认'),
        ('review', '等待他人复核'),
    ]

    # 两段式：发起人先确认（限本人、短过期），需要复核时再发一张等人复核的票据
    #（不限人——谁够格谁点，但**不能是发起人自己**，否则复核就是走个过场）
    stage = models.CharField('阶段', max_length=20, choices=STAGE_CHOICES, default='confirm')
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name='复核人', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='feishu_reviewed_tokens',
    )
    open_id = models.CharField('发起人 OpenID', max_length=128)
    chat_id = models.CharField('会话 ID', max_length=128, blank=True)
    # 本次要执行的主机 id（空 = 全部匹配主机）。**存服务端不放卡片**：卡片上的
    # 值只用于展示，而这批 id 决定了生产上到底动哪几台，必须由签发方持有
    server_ids = models.JSONField('已选主机', default=list, blank=True)
    expires_at = models.DateTimeField('过期时间')
    used_at = models.DateTimeField('消费时间', null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = '飞书确认票据'
        verbose_name_plural = '飞书确认票据'
        db_table = 'im_feishu_confirm_token'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.token[:8]}…({self.task_id})'

    @classmethod
    def issue(cls, task, user, open_id, chat_id, ttl_seconds, server_ids=None,
              stage='confirm'):
        """签发一张票据，顺手清掉过期一天以上的旧票据。"""
        from django.utils import timezone

        now = timezone.now()
        cls.objects.filter(expires_at__lt=now - timedelta(days=1)).delete()
        return cls.objects.create(
            token=secrets.token_urlsafe(24), task=task, user=user,
            # 复核票据不限 open_id：谁够格谁点。「不能是发起人自己」在消费时判，
            # 因为那要比的是平台用户而不是飞书身份（换个飞书号绕不过去）
            open_id='' if stage == 'review' else (open_id or ''),
            chat_id=chat_id or '', stage=stage,
            server_ids=list(server_ids or []),
            expires_at=now + timedelta(seconds=ttl_seconds),
        )

    @classmethod
    def consume(cls, token, open_id):
        """消费票据，返回 (票据, 中文错误)；错误非空时未做任何写入。

        先查后条件更新：直接用一条 update 也能保证一次性，但那样所有失败原因都
        会塌成「无效或已过期」，出问题时没法判断是点晚了、点错人了还是重复点。
        并发安全由 `used_at__isnull=True` 的条件更新保证，不靠这次查询。
        """
        from django.utils import timezone

        row = cls.objects.select_related('task', 'user').filter(token=str(token or '')).first()
        if row is None:
            return None, '确认票据无效，请重新发起执行。'
        if row.open_id and row.open_id != (open_id or ''):
            return None, '只有发起人本人可以确认这次生产执行。'
        if row.used_at is not None:
            return None, '该确认已处理过，请勿重复点击。'
        if row.expires_at <= timezone.now():
            return None, '确认已超时，请重新发起执行。'
        updated = cls.objects.filter(pk=row.pk, used_at__isnull=True).update(
            used_at=timezone.now())
        if not updated:
            return None, '该确认已处理过，请勿重复点击。'
        return row, None


class FeishuCardMessage(models.Model):
    """一次执行 ↔ 那张卡片消息的对应关系。

    执行进度靠**改同一条消息**呈现，不是不停发新消息——飞书不适合流式日志，
    刷屏的代价比看不到中间态高得多。要改就得记住 message_id：飞书只认这个。

    一次执行只绑一张卡（OneToOne）。从飞书发起时绑的是「被按钮替换掉的那条
    消息」，所以群里始终是同一条消息在原地变化。页面发起的任务在转「等待处理」
    时会私聊推一张卡给发起人并绑上，此时 `chat_id` 存的是收件人的 `open_id`
    ——更新卡片只认 `message_id`，这个字段只作留痕。
    日志删除时一并删掉（CASCADE）：留着一条指向不存在执行的映射没有任何用处。
    """

    log = models.OneToOneField(
        'task_exec.TaskExecutionLog', verbose_name='执行日志',
        on_delete=models.CASCADE, related_name='feishu_card',
    )
    chat_id = models.CharField('会话 ID', max_length=128)
    message_id = models.CharField('消息 ID', max_length=128)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = '飞书执行卡片'
        verbose_name_plural = '飞书执行卡片'
        db_table = 'im_feishu_card_message'
        ordering = ['-created_at']

    def __str__(self):
        return f'log={self.log_id} msg={self.message_id}'


class FeishuSudoRequest(models.Model):
    """sudo 卡片确认的一次待确认请求。

    解决的问题：`SudoView` 要求验证器验证码，没绑则回退登录密码——而飞书登录
    自动创建的账号**两者都没有**（不设可用密码是刻意的），于是做不了任何配置面的
    写操作。给这类账号一条「在飞书上确认一次」的路。

    安全上与生产执行确认同一套做法：一次性、限本人 `open_id`、短过期，并且
    **绑定发起它的那个会话**（`jti`）——否则 A 会话发起、B 会话消费，等于把
    「刚刚证明过是本人」这件事挪到了另一条会话上。

    卡片上写明**请求来源 IP、时间与要做的操作**：卡片确认的摩擦比翻验证器小，
    容易被随手点掉，写清楚是唯一能让人「看一眼再点」的手段。
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name='用户', on_delete=models.CASCADE,
        related_name='feishu_sudo_requests',
    )
    token = models.CharField('票据', max_length=64, unique=True)
    # 发起这次请求的访问令牌 jti：sudo 窗口就是按它开的（见 common/sudo.py）
    jti = models.CharField('会话标识', max_length=64)
    open_id = models.CharField('飞书 OpenID', max_length=128)
    action = models.CharField('操作说明', max_length=200, blank=True)
    client_ip = models.CharField('请求来源 IP', max_length=64, blank=True)
    expires_at = models.DateTimeField('过期时间')
    approved_at = models.DateTimeField('确认时间', null=True, blank=True)
    consumed_at = models.DateTimeField('消费时间', null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = '飞书 sudo 确认'
        verbose_name_plural = '飞书 sudo 确认'
        db_table = 'im_feishu_sudo_request'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.user_id}#{self.token[:8]}…'

    @property
    def expired(self):
        from django.utils import timezone

        return self.expires_at <= timezone.now()

    @property
    def state(self):
        """pending / approved / consumed / expired。"""
        if self.consumed_at:
            return 'consumed'
        if self.approved_at:
            return 'approved'
        return 'expired' if self.expired else 'pending'

    @classmethod
    def issue(cls, user, jti, open_id, ttl_seconds, action='', client_ip=''):
        from django.utils import timezone

        now = timezone.now()
        cls.objects.filter(expires_at__lt=now - timedelta(days=1)).delete()
        # 同一会话的旧请求作废：留着的话，用户连点两次会收到两张卡，
        # 点了旧的那张也能开窗口，等于过期时间被悄悄延长
        cls.objects.filter(user=user, jti=jti, consumed_at__isnull=True).update(
            consumed_at=now)
        return cls.objects.create(
            user=user, token=secrets.token_urlsafe(24), jti=jti or '',
            open_id=open_id or '', action=action[:200], client_ip=client_ip[:64],
            expires_at=now + timedelta(seconds=ttl_seconds),
        )

    @classmethod
    def approve(cls, token, open_id):
        """飞书侧点确认；返回 (请求, 中文错误)。"""
        from django.utils import timezone

        row = cls.objects.select_related('user').filter(token=str(token or '')).first()
        if row is None:
            return None, '确认请求不存在，请回到页面重新发起。'
        if row.open_id and row.open_id != (open_id or ''):
            return None, '只有发起人本人可以确认这次操作。'
        if row.consumed_at:
            return None, '该确认已处理过。'
        if row.approved_at:
            return None, '已确认过了，请回到页面继续操作。'
        if row.expired:
            return None, '确认已超时，请回到页面重新发起。'
        updated = cls.objects.filter(pk=row.pk, approved_at__isnull=True,
                                     consumed_at__isnull=True).update(
            approved_at=timezone.now())
        if not updated:
            return None, '该确认已处理过。'
        row.refresh_from_db()
        return row, None

    def consume(self):
        """标记为已消费（开完 sudo 窗口后调用），返回是否由本次消费掉。"""
        from django.utils import timezone

        return bool(type(self).objects.filter(
            pk=self.pk, consumed_at__isnull=True).update(consumed_at=timezone.now()))
