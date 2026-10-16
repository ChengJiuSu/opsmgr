"""消息通知：发送目标（群机器人）、订阅规则、通知事件与发送记录。

四张表各管一件事：

- `WebhookRobot`：飞书自定义群机器人。只要一个 Webhook token 就能往群里发，
  不需要飞书应用、也不需要公网回调——只想「失败了群里吱一声」的场景用它；
- `NotifyRule`：订阅规则。「哪些事件 × 哪些维度 → 发给谁」，两种发送目标
  （应用机器人会话 / 群机器人）共用这一套规则，按需二选一或都配；
- `NotifyEvent`：业务方 emit 的一次事件。`dedupe_key` 唯一 = 同一件事只通知
  一次（终态会被状态推送与模块表写入重复广播）；
- `NotifyDelivery`：一条事件发给一个目标的结果。排查「为什么没收到」就看它。

维度订阅与平台其余地方同一套口径（项目组 / 渠道 / 环境，空 = 不限），不引入
新概念：一个群只关心自己那摊事，别的项目组的失败播过来只会被当噪声。
"""
from django.db import models

from common.fields import EncryptedTextField


class WebhookRobot(models.Model):
    """飞书自定义群机器人（Webhook）。

    `hook_token` 就是 Webhook 地址 `/open-apis/bot/v2/hook/<token>` 的最后一段。
    **只存 token 不存整条地址**：地址由 settings.FEISHU_BASE_URL 拼出来，后台
    没法填一个任意 URL 让服务器去请求（SSRF）。token 本身等于发消息的凭据，
    加密存、接口不回显。
    """

    name = models.CharField('机器人名称', max_length=100, unique=True)
    hook_token = EncryptedTextField('Webhook token')
    # 机器人「安全设置」里开了签名校验才需要；没开时带签名飞书也照收
    secret = EncryptedTextField('签名密钥', blank=True)
    enabled = models.BooleanField('启用', default=True)
    remark = models.CharField('备注', max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = '群机器人'
        verbose_name_plural = '群机器人'
        db_table = 'notify_webhook_robot'
        ordering = ['name']

    def __str__(self):
        return self.name

    @property
    def has_secret(self):
        return bool(self.secret)

    @property
    def masked_token(self):
        """回显用：只露头尾各 4 位，够认出是哪个机器人，不够拿去发消息。"""
        token = self.hook_token or ''
        return f'{token[:4]}****{token[-4:]}' if len(token) > 8 else '****'


class NotifyRule(models.Model):
    """订阅规则：哪些事件、哪些维度的通知发给哪个目标。"""

    TARGET_FEISHU_CHAT = 'feishu_chat'
    TARGET_FEISHU_WEBHOOK = 'feishu_webhook'
    TARGET_CHOICES = [
        (TARGET_FEISHU_CHAT, '应用机器人'),
        (TARGET_FEISHU_WEBHOOK, '群机器人'),
    ]

    name = models.CharField('规则名称', max_length=100)
    enabled = models.BooleanField('启用', default=True)
    # 事件码列表（见 events.REGISTRY）。存码不存外键：事件类型是代码里注册的，
    # 不是数据——加一种事件不该需要一次数据迁移
    events = models.JSONField('订阅事件', default=list)
    project_groups = models.ManyToManyField(
        'asset_mgr.ProjectGroup', verbose_name='项目组（空=不限）', blank=True,
        related_name='notify_rules')
    channels = models.ManyToManyField(
        'asset_mgr.Channel', verbose_name='渠道（空=不限）', blank=True,
        related_name='notify_rules')
    environments = models.ManyToManyField(
        'asset_mgr.Environment', verbose_name='环境（空=不限）', blank=True,
        related_name='notify_rules')
    target_type = models.CharField('发送方式', max_length=20, choices=TARGET_CHOICES)
    # 规则从属于它的发送目标：会话或机器人删了，发给它的规则也就没有意义了
    feishu_chat = models.ForeignKey(
        'im_mgr.FeishuChannel', verbose_name='应用机器人会话', on_delete=models.CASCADE,
        null=True, blank=True, related_name='notify_rules')
    webhook_robot = models.ForeignKey(
        WebhookRobot, verbose_name='群机器人', on_delete=models.CASCADE,
        null=True, blank=True, related_name='rules')
    # @ 发起人：要求发起人绑定了飞书账号（FeishuBinding.open_id），否则不 @
    mention_trigger = models.BooleanField('@ 发起人', default=False)
    remark = models.CharField('备注', max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = '订阅规则'
        verbose_name_plural = '订阅规则'
        db_table = 'notify_rule'
        ordering = ['name', 'id']

    def __str__(self):
        return self.name

    @property
    def target(self):
        if self.target_type == self.TARGET_FEISHU_CHAT:
            return self.feishu_chat
        return self.webhook_robot

    @property
    def target_key(self):
        """发送目标的去重键：两条规则指向同一个会话时，同一事件只发一次。"""
        if self.target_type == self.TARGET_FEISHU_CHAT:
            return f'chat:{self.feishu_chat.chat_id}' if self.feishu_chat_id else ''
        return f'webhook:{self.webhook_robot_id}' if self.webhook_robot_id else ''

    @property
    def target_name(self):
        target = self.target
        return getattr(target, 'name', '') if target else ''

    def target_ready(self):
        """目标本身可用（存在且启用）。飞书应用是否就绪在发送时判。"""
        target = self.target
        return bool(target and target.enabled)

    def matches(self, event):
        """该规则是否订阅了这次事件：事件码命中、三个维度都相容（空 = 不限）。

        维度用预取后的 id 集合判：规则数量是几十条的量级，逐条查库反而更慢。
        """
        if event.code not in (self.events or []):
            return False
        for field, value in (('project_groups', event.project_group_id),
                             ('channels', event.channel_id),
                             ('environments', event.environment_id)):
            allowed = {obj.pk for obj in getattr(self, field).all()}
            if allowed and value not in allowed:
                return False
        return True


class NotifyEvent(models.Model):
    """业务方发出的一次通知事件（见 events.emit）。

    维度存 id 不存外键：事件是历史记录，字典项删了不该连带删掉或卡住它。
    """

    code = models.CharField('事件', max_length=64, db_index=True)
    dedupe_key = models.CharField('去重键', max_length=200, unique=True)
    source_type = models.CharField('来源类型', max_length=64)
    source_id = models.CharField('来源 ID', max_length=64)
    project_group_id = models.IntegerField('项目组 ID', null=True, blank=True)
    channel_id = models.IntegerField('渠道 ID', null=True, blank=True)
    environment_id = models.IntegerField('环境 ID', null=True, blank=True)
    # 事件发生那一刻才有的信息（如终止原因）。渲染在发送时现读库，
    # 但「当时是什么情况」库里不一定还留着，所以随事件带上
    context = models.JSONField('上下文', default=dict, blank=True)
    created_at = models.DateTimeField('发生时间', auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = '通知事件'
        verbose_name_plural = '通知事件'
        db_table = 'notify_event'
        ordering = ['-created_at', '-id']
        indexes = [models.Index(fields=['source_type', 'source_id'])]

    def __str__(self):
        return f'{self.code}#{self.source_id}'


class NotifyDelivery(models.Model):
    """一次事件发给一个目标的结果。"""

    STATUS_CHOICES = [
        ('pending', '发送中'),
        ('success', '已发送'),
        ('failed', '发送失败'),
        ('skipped', '已跳过'),
    ]

    event = models.ForeignKey(
        NotifyEvent, on_delete=models.CASCADE, related_name='deliveries', verbose_name='事件')
    rule = models.ForeignKey(
        NotifyRule, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='deliveries', verbose_name='命中规则')
    target_type = models.CharField('发送方式', max_length=20, choices=NotifyRule.TARGET_CHOICES)
    target_key = models.CharField('目标', max_length=200)
    # 快照：规则或目标删了以后，记录里还要看得出当时发给了谁
    rule_name = models.CharField('规则名称', max_length=100, blank=True)
    target_name = models.CharField('目标名称', max_length=100, blank=True)
    title = models.CharField('消息标题', max_length=200, blank=True)
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    error = models.TextField('失败原因', blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    sent_at = models.DateTimeField('发送时间', null=True, blank=True)

    class Meta:
        verbose_name = '发送记录'
        verbose_name_plural = '发送记录'
        db_table = 'notify_delivery'
        ordering = ['-created_at', '-id']
        constraints = [
            models.UniqueConstraint(fields=['event', 'target_key'], name='uniq_notify_event_target'),
        ]

    def __str__(self):
        return f'{self.event_id}->{self.target_key}:{self.status}'
