"""把超时残留的证书签发/分发状态标为失败。

签发（issuing）与分发（deploying）跑在 gunicorn worker 的守护线程里，进程重启或
worker 回收会把线程杀掉，状态就永久留在库里；而 issuing 既禁止重复触发又禁止删除，
那条记录等于死锁——重启也不会自愈，只能靠这个命令（或进程启动时的自动收尾）。

建议挂 cron 每 10 分钟执行：

    python manage.py reap_stuck_cert_tasks
"""
from django.core.management.base import BaseCommand

from apps.cloud_mgr.tasks import reap_stuck


class Command(BaseCommand):
    help = '把超时未回执的证书签发（issuing）/ 分发（deploying）标为失败'

    def handle(self, *args, **options):
        cert_count, deploy_count = reap_stuck()
        self.stdout.write(self.style.SUCCESS(
            f'已收回残留：证书签发 {cert_count} 条，证书分发 {deploy_count} 条',
        ))
