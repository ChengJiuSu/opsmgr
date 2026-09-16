"""飞书接入自检（联调用，只读为主）。

**在去飞书后台保存回调地址之前跑它**。那一步一保存就立刻触发地址验证，而验证
失败时回包是恒定的（安全要求），从飞书后台完全看不出原因——只能翻服务端日志。
先用这条命令把「平台侧准备好了没」一次问清楚，比对着日志猜快得多。

    ./venv/bin/python manage.py feishu_check                    # 配置 + 出站连通性
    ./venv/bin/python manage.py feishu_check --chats            # 列出机器人所在的群与 chat_id
    ./venv/bin/python manage.py feishu_check --email a@b.com    # 顺带验通讯录反查
    ./venv/bin/python manage.py feishu_check --to <用户名>      # 真发一张测试卡片（私聊）
    ./venv/bin/python manage.py feishu_check --to oc_xxx        # 发到群里

除 `--chat-id` 外全部只读，不会发任何消息、不改任何数据。
"""
from django.conf import settings
from django.core.management.base import BaseCommand

from apps.im_mgr import client, sso
from apps.im_mgr.models import FeishuApp

OK = '  [ok] '
BAD = '  [!!] '
INFO = '       '


class Command(BaseCommand):
    help = '检查飞书接入配置与出站连通性（联调自检）'

    def add_arguments(self, parser):
        parser.add_argument('--email', help='用该邮箱验证通讯录反查（batch_get_id）')
        parser.add_argument('--mobile', help='用该手机号验证通讯录反查')
        parser.add_argument('--chats', action='store_true',
                            help='列出机器人已加入的群与 chat_id（登记会话白名单用）')
        parser.add_argument('--chat-id', dest='to',
                            help='测试卡片的收件人：群 chat_id（oc_…）/ 飞书 open_id（ou_…）'
                                 '/ 平台用户名（自动查其飞书绑定）')
        parser.add_argument('--to', dest='to', help='同 --chat-id')
        parser.add_argument('--probe-select', action='store_true',
                            help='测试卡改发「多选组件探针」，用来确认飞书回传的结构')

    def handle(self, *args, **options):
        self._warn_settings_module()
        app = FeishuApp.load()
        failed = self._check_config(app)
        if failed:
            self.stdout.write(self.style.ERROR(
                '\n配置未就绪，后面的检查跳过。先在「系统管理 → 飞书接入」补齐。'))
            return

        failed |= self._check_outbound(app)
        failed |= self._print_urls(app)
        if options.get('chats'):
            failed |= self._list_chats()
        if options.get('email') or options.get('mobile'):
            failed |= self._check_lookup(options.get('email'), options.get('mobile'))
        if options.get('to'):
            failed |= self._check_send(options['to'], options.get('probe_select'))

        self.stdout.write('')
        if failed:
            self.stdout.write(self.style.ERROR('存在未通过项，见上面的 [!!]。'))
        else:
            self.stdout.write(self.style.SUCCESS(
                '全部通过。可以去飞书开放平台保存回调地址了（顺序见 deploy/README.md）。'))

    # ── 环境 ─────────────────────────────────────────────
    @staticmethod
    def _warn_settings_module():
        """连的是哪个库，先说清楚。

        manage.py 默认 DJANGO_SETTINGS_MODULE=…settings.dev，而 dev 用 SQLite。
        在生产机上不带 --settings 直接跑，读到的是一个空的 db.sqlite3，报出来是
        「四项凭据全缺」——指向完全错误的方向（配置明明在页面上好好的）。
        """
        import os

        from django.db import connection

        module = os.environ.get('DJANGO_SETTINGS_MODULE', '(未设置)')
        engine = connection.settings_dict.get('ENGINE', '').rsplit('.', 1)[-1]
        name = connection.settings_dict.get('NAME', '')
        line = f'settings={module}　数据库={engine}:{name}'
        if engine == 'sqlite3':
            print(f'  [!!] {line}')
            print('       生产上要带 --settings=ops_manager.settings.prod，'
                  '否则读的是空的本地 SQLite')
        else:
            print(f'       {line}')

    # ── 配置 ─────────────────────────────────────────────
    def _check_config(self, app):
        self.stdout.write(self.style.MIGRATE_HEADING('[1] 配置完整性'))
        missing = app.missing_fields()
        if missing:
            self.stdout.write(BAD + f'缺少：{"、".join(missing)}')
            return True
        self.stdout.write(OK + '四项凭据与 IP 策略齐全')
        if not app.enabled:
            self.stdout.write(BAD + '「启用飞书接入」未打开，回调会被拒')
            return True
        self.stdout.write(OK + '飞书接入已启用')
        if app.ip_unrestricted:
            # 不算失败，但这是个需要有人点头的决定，不能静默放过去
            self.stdout.write(INFO + '注意：已勾选「不限制来源 IP」，应用层白名单不生效')
        else:
            self.stdout.write(OK + f'来源 IP 白名单：{app.ip_whitelist.strip()}')
        self.stdout.write(INFO + f'飞书登录：{"已启用" if app.sso_enabled else "未启用"}；'
                                 f'自动建号：{"开" if app.auto_create_user else "关"}')
        return False

    # ── 出站 ─────────────────────────────────────────────
    def _check_outbound(self, app):
        self.stdout.write(self.style.MIGRATE_HEADING('\n[2] 出站连通性'))
        self.stdout.write(INFO + f'开放平台：{settings.FEISHU_BASE_URL}　'
                                 f'走环境代理：{settings.FEISHU_TRUST_ENV}')
        failed = False
        for label, getter in (('tenant_access_token（发消息/通讯录）', client.get_tenant_token),
                              ('app_access_token（网页免登）', client.get_app_token)):
            try:
                getter(app, force_refresh=True)
                self.stdout.write(OK + f'{label} 取到了')
            except client.FeishuError as exc:
                self.stdout.write(BAD + f'{label} 失败：{exc}')
                failed = True
        return failed

    # ── 地址 ─────────────────────────────────────────────
    def _print_urls(self, app):
        """把要粘到飞书后台的三个地址打出来，顺带暴露 FEISHU_AUTHORIZE_PATH。

        授权页路径飞书换过名字（旧版 /open-apis/authen/v1/index），联调时最先
        撞上的就是它，所以原样打出来让人一眼核对。
        """
        self.stdout.write(self.style.MIGRATE_HEADING('\n[3] 需要填到飞书后台的地址'))
        base = settings.SITE_BASE_URL.rstrip('/')
        failed = False
        self.stdout.write(INFO + f'事件订阅：  {base}{app.event_path}')
        self.stdout.write(INFO + f'卡片回调：  {base}{app.card_path}')
        if app.sso_enabled:
            try:
                self.stdout.write(INFO + f'重定向 URL：{sso.redirect_uri(app)}')
            except sso.SsoError:
                # 没配死地址时是按请求域名推导的，命令行里没有 request
                self.stdout.write(INFO + f'重定向 URL：{base}{sso.DEFAULT_REDIRECT_PATH}'
                                         f'（未配置，按 SITE_BASE_URL 推导；实际以浏览器域名为准）')
        failed |= self._probe_authorize_path(app)
        if base.startswith('http://127.0.0.1') or not base:
            self.stdout.write(BAD + 'SITE_BASE_URL 还是默认值，上面的地址不可用，请在 .env 配置')
            failed = True
        return failed

    def _probe_authorize_path(self, app):
        """探一下授权页路径在不在。

        飞书把这条地址换过名字（旧版 /open-apis/authen/v1/index），联调时最先撞上
        的就是它，而现象是「浏览器跳过去一片空白」——没有任何服务端日志。这里用
        一次不跟随重定向的 GET 把状态码打出来：404 基本就是路径不对。
        """
        import requests

        path = settings.FEISHU_AUTHORIZE_PATH
        url = settings.FEISHU_BASE_URL.rstrip('/') + path
        try:
            resp = requests.get(
                url, params={'app_id': app.app_id, 'redirect_uri': settings.SITE_BASE_URL,
                             'response_type': 'code', 'state': 'probe'},
                allow_redirects=False, timeout=settings.FEISHU_TIMEOUT)
        except requests.RequestException as exc:
            # 探不通不算失败：出站代理可能只放行了 API 域名的部分路径
            self.stdout.write(INFO + f'授权页路径：{path}（探测失败：{exc}）')
            return False
        if resp.status_code == 404:
            self.stdout.write(BAD + f'授权页路径 {path} 返回 404，改 FEISHU_AUTHORIZE_PATH '
                                    f'（旧版为 /open-apis/authen/v1/index）')
            return True
        self.stdout.write(OK + f'授权页路径 {path} 可达（HTTP {resp.status_code}）')
        return False

    # ── 群列表 ───────────────────────────────────────────
    def _list_chats(self):
        """列出机器人所在的群——登记会话白名单要 chat_id，飞书界面上看不到。"""
        self.stdout.write(self.style.MIGRATE_HEADING('\n[4] 机器人已加入的群'))
        try:
            chats = client.list_chats()
        except client.FeishuError as exc:
            self.stdout.write(BAD + f'查询失败：{exc}')
            self.stdout.write(INFO + '多半是缺 im:chat:readonly 权限，或机器人能力未启用')
            return True
        if not chats:
            self.stdout.write(BAD + '机器人还没被拉进任何群')
            self.stdout.write(INFO + '群设置 → 群机器人 → 添加机器人，选中本应用')
            return True
        for chat in chats:
            self.stdout.write(OK + f'{chat.get("name") or "(无名群)"}　{chat.get("chat_id")}')
        self.stdout.write(INFO + '把要用的 chat_id 登记到「系统管理 → 飞书接入 → 会话白名单」')
        return False

    # ── 通讯录反查 ───────────────────────────────────────
    def _check_lookup(self, email, mobile):
        self.stdout.write(self.style.MIGRATE_HEADING('\n[5] 通讯录反查'))
        try:
            ids = client.lookup_user_ids(email=email, mobile=mobile)
        except client.FeishuError as exc:
            self.stdout.write(BAD + f'反查失败：{exc}')
            self.stdout.write(INFO + '多半是缺 contact:user.base:readonly 权限')
            return True
        if not ids:
            self.stdout.write(BAD + '通讯录里没查到这个人（邮箱/手机号是否为企业账号？）')
            return True
        self.stdout.write(OK + f'union_id={ids.get("union_id")} open_id={ids.get("open_id")}')
        try:
            profile = client.get_user(ids['open_id'])
        except client.FeishuError as exc:
            self.stdout.write(BAD + f'取用户详情失败：{exc}（缺 contact:user.email:readonly？）')
            return True
        # 邮箱字段名是 SSO 自动绑定的依据，联调必须当场确认
        self.stdout.write(OK + f'姓名={profile.get("name")}　'
                               f'enterprise_email={profile.get("enterprise_email")!r}　'
                               f'email={profile.get("email")!r}')
        if not (profile.get('enterprise_email') or profile.get('email')):
            self.stdout.write(BAD + '两个邮箱字段都空：飞书登录将无法按邮箱自动绑定用户')
            return True
        return False

    # ── 发卡 ─────────────────────────────────────────────
    @staticmethod
    def _resolve_target(target):
        """收件人 → (receive_id, receive_id_type)。

        单聊场景下让人去翻自己的 open_id 不现实（飞书界面上不显示），所以既收
        ID 也收平台用户名——用户名走飞书绑定表反查。`oc_`/`ou_` 前缀是飞书的
        固定约定，可以直接判；注意**单聊的 chat_id 同样是 oc_ 开头**，但那也确实
        该按 chat_id 发，不冲突。
        """
        from apps.im_mgr.models import FeishuBinding

        target = str(target or '').strip()
        if target.startswith('oc_'):
            return target, 'chat_id'
        if target.startswith('ou_'):
            return target, 'open_id'
        binding = FeishuBinding.objects.filter(user__username=target).first()
        if binding is None or not binding.open_id:
            return None, f'平台用户「{target}」没有飞书绑定，取不到收件人'
        return binding.open_id, 'open_id'

    def _check_send(self, target, probe_select=False):
        from apps.im_mgr import cards

        self.stdout.write(self.style.MIGRATE_HEADING('\n[6] 发送测试卡片'))
        receive_id, receive_id_type = self._resolve_target(target)
        if receive_id is None:
            self.stdout.write(BAD + receive_id_type)   # 这一支里第二个返回值是原因
            return True
        self.stdout.write(INFO + f'收件人：{receive_id}（{receive_id_type}）')
        if probe_select:
            card = cards.probe_multi_select([
                {'label': 'web1 · 10.0.0.1', 'value': '1'},
                {'label': 'web2 · 10.0.0.2', 'value': '2'},
                {'label': 'web3 · 10.0.0.3', 'value': '3'},
            ])
        else:
            card = cards.notice('飞书接入自检', '这是一张来自运维平台的测试卡片，收到即表示出站正常。',
                                tip='由 manage.py feishu_check 发出')
        try:
            message_id = client.send_card(receive_id, card,
                                          receive_id_type=receive_id_type)
        except client.FeishuError as exc:
            self.stdout.write(BAD + f'发送失败：{exc}')
            # 卡片 JSON 被拒时，「机器人不在群里」这类提示纯属误导——飞书自己
            # 已经把出错的属性和路径指出来了，照着改就行
            if 'card json' in str(exc).lower() or 'unknown property' in str(exc).lower():
                self.stdout.write(INFO + '卡片结构被飞书拒了（严格校验，多一个属性都不行）：'
                                         '按上面的 property / path 改卡片模板')
            else:
                self.stdout.write(INFO + '常见原因：机器人不在该群 / 对方没跟机器人建立过会话、'
                                         '或缺 im:message:send_as_bot 权限')
            return True
        self.stdout.write(OK + f'已发出，message_id={message_id}')
        if probe_select:
            self.stdout.write(INFO + '在飞书里改几项再点「提交」，回传结构会直接显示在卡片上，'
                                     '也会进日志（grep 组件探针）')
        return False
