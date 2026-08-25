"""清除指定用户的 MFA 绑定（服务器 shell 兜底）。

存在意义：全员强制 MFA 后，如果最后一个管理员也丢了验证器，Web 界面上就没
人能给他重置了——必须留一条不依赖登录的通道。开启强制**之前**就要确认这条
命令可用。

    ./venv/bin/python manage.py mfa_reset <username> [--yes]
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.auth_mgr import services

User = get_user_model()


class Command(BaseCommand):
    help = '清除用户的 MFA 绑定与恢复码，用户下次登录需重新绑定'

    def add_arguments(self, parser):
        parser.add_argument('username')
        parser.add_argument('--yes', action='store_true', help='跳过交互确认')

    def handle(self, *args, **options):
        username = options['username']
        user = User.objects.filter(username=username).first()
        if user is None:
            raise CommandError(f'用户不存在：{username}')
        binding = services.get_binding(user)
        if binding is None:
            self.stdout.write(f'用户 {username} 未绑定 MFA，无需处理')
            return
        state = '已启用' if binding.enabled else '待激活'
        if not options['yes']:
            answer = input(f'确认清除用户 {username} 的 MFA 绑定（{state}）？[y/N] ')
            if answer.strip().lower() not in ('y', 'yes'):
                self.stdout.write('已取消')
                return
        services.reset(user)
        self.stdout.write(self.style.WARNING(
            f'已清除 {username} 的 MFA 绑定（{state}）。该用户下次登录需重新绑定。'))
