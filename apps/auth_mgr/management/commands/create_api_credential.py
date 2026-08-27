"""签发一把 API 凭证（命令行版，用于首次上线时给存量脚本换 key）。

页面上建凭证要过 sudo 二次验证，而上线当天往往还没人登得进来（或者要在
部署脚本里批量签发），所以留一条命令行通道。明文只打印一次。

    ./venv/bin/python manage.py create_api_credential \
        --name ci-jenkins --scopes artifact:report --ip 10.0.1.0/24,10.0.2.5
"""
from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_datetime

from common.request import parse_ip_rules

from ...apikey import SCOPES, ensure_service_user
from ...models import ApiCredential


class Command(BaseCommand):
    help = '签发一把 API 凭证并打印明文 key（仅此一次可见）'

    def add_arguments(self, parser):
        parser.add_argument('--name', required=True, help='凭证名称（唯一）')
        parser.add_argument('--scopes', required=True,
                            help=f'作用域，逗号分隔。可选：{",".join(SCOPES)}')
        parser.add_argument('--ip', default='',
                            help='来源 IP 白名单（IP 或 CIDR，逗号分隔）')
        parser.add_argument('--allow-any-ip', action='store_true',
                            help='不限制来源 IP（危险，必须显式指定）')
        parser.add_argument('--rate', default='', help='限速，如 60/min；留空用全局默认')
        parser.add_argument('--expires', default='', help='过期时间，如 2026-12-31T23:59:59')
        parser.add_argument('--remark', default='', help='备注')

    def handle(self, *args, **options):
        name = options['name'].strip()
        if ApiCredential.objects.filter(name=name).exists():
            raise CommandError(f'凭证名称已存在：{name}')

        scopes = [item.strip() for item in options['scopes'].split(',') if item.strip()]
        unknown = [item for item in scopes if item not in SCOPES]
        if not scopes or unknown:
            raise CommandError(f'作用域非法：{unknown or "(空)"}，可选 {",".join(SCOPES)}')

        whitelist = options['ip'].strip()
        unrestricted = options['allow_any_ip']
        # 与页面同一条规则：留空不等于不限制，必须显式声明（见序列化器 _validate_ip）
        if not unrestricted and not whitelist:
            raise CommandError('必须给 --ip 白名单；确实不限制来源请显式加 --allow-any-ip')
        if unrestricted:
            whitelist = ''
        else:
            try:
                parse_ip_rules(whitelist)
            except ValueError as exc:
                raise CommandError(f'白名单条目非法：{exc}') from exc

        expires_at = None
        if options['expires']:
            expires_at = parse_datetime(options['expires'])
            if expires_at is None:
                raise CommandError('--expires 格式非法，示例：2026-12-31T23:59:59')

        raw, key_id, secret_hash = ApiCredential.generate_key()
        credential = ApiCredential.objects.create(
            name=name, key_id=key_id, secret_hash=secret_hash, scopes=sorted(scopes),
            ip_unrestricted=unrestricted, ip_whitelist=whitelist,
            rate_limit=options['rate'].strip(), expires_at=expires_at,
            bound_user=ensure_service_user(name), remark=options['remark'],
        )
        self.stdout.write(self.style.SUCCESS(f'已签发凭证 {credential.name}'))
        self.stdout.write(f'  key_id   : {credential.key_id}')
        self.stdout.write(f'  作用域   : {"、".join(credential.scopes)}')
        self.stdout.write(f'  IP 白名单: {"不限制（危险）" if unrestricted else whitelist}')
        self.stdout.write(f'  服务账号 : {credential.bound_user.username}')
        self.stdout.write(self.style.WARNING(f'\n  API Key（仅此一次可见）: {raw}\n'))
        self.stdout.write('调用示例：curl -H "X-API-KEY: <上面的 key>" ...')
