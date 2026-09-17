"""飞书长连接接收进程（常驻）。

    ./venv/bin/python manage.py feishu_ws --settings=ops_manager.settings.prod

平台主动外连飞书，**入站零暴露**——服务可以收回内网、只对指定 IP 开放，
`/api/feishu/callback/` 那两条公网入口不再需要。代价是多一个常驻进程
（supervisor 配置见 deploy/ops-manager-feishu-ws.conf）。

与回调模式**互斥**：两边都开着，同一个事件会被处理两次。所以本命令会先检查
「系统管理 → 飞书接入」里的事件接收方式，不是长连接就直接退出，而不是默默连上去。
"""
import os

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = '启动飞书长连接事件接收进程（常驻）'

    def add_arguments(self, parser):
        parser.add_argument('--force', action='store_true',
                            help='即使配置里选的是回调模式也启动（调试用）')

    def handle(self, *args, **options):
        # SDK 在 asyncio 事件循环里同步调用 handler，Django ORM 在异步上下文中
        # 默认会抛 SynchronousOnlyOperation。这里明确放行——由此也带来一条硬约束：
        # 执行后端必须是 celery，sync 后端会把 ansible 跑在事件循环里卡死连接
        os.environ.setdefault('DJANGO_ALLOW_ASYNC_UNSAFE', 'true')

        from django.conf import settings

        from apps.im_mgr import ws
        from apps.im_mgr.models import FeishuApp

        app = FeishuApp.load()
        if not app.is_ready():
            missing = app.missing_fields()
            raise CommandError(
                f'飞书接入未就绪：{"、".join(missing) if missing else "未启用"}')
        if not app.use_websocket and not options['force']:
            raise CommandError(
                '当前事件接收方式是「回调」。请先到「系统管理 → 飞书接入」改成'
                '「长连接」再启动本进程——两种模式同时开启会让同一个事件被处理两次。')

        backend = getattr(settings, 'TASK_EXEC_BACKEND', 'thread')
        if backend == 'sync':
            raise CommandError(
                'TASK_EXEC_BACKEND=sync 与长连接不能共存：任务会在事件循环里同步'
                '执行，整条连接被卡死。生产请用 celery。')
        if backend != 'celery':
            self.stdout.write(self.style.WARNING(
                f'  [!!] TASK_EXEC_BACKEND={backend}，生产建议用 celery'))

        self.stdout.write(f'飞书长连接启动：app_id={app.app_id}')
        try:
            ws.build_client(app).start()
        except KeyboardInterrupt:
            self.stdout.write('已停止。')
