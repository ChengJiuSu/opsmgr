"""飞书长连接接收进程（常驻）。

    ./venv/bin/python manage.py feishu_ws --settings=ops_manager.settings.prod

平台主动外连飞书，**入站零暴露**——服务可以收回内网、只对指定 IP 开放，
`/api/feishu/callback/` 那两条公网入口不再需要。代价是多一个常驻进程
（supervisor 配置见 deploy/ops-manager-feishu-ws.conf）。

与回调模式**互斥**：两边都开着，同一个事件会被处理两次。所以本命令会先检查
「系统管理 → 飞书接入」里的事件接收方式，不是长连接就不连上去。

── 未就绪时为什么是等待而不是退出 ───────────────────────────────────────────
「飞书没配」「当前选的是回调模式」这两种状态都存在于**数据库**里，运维随时
会在页面上改。早先的实现是直接 CommandError 退出，而进程管理器一律配了
自动重启（supervisor autorestart=true / compose restart: unless-stopped），
结果是每隔几秒重启一次、刷同一条报错，直到有人去改配置——既烧重启计数，
也把真正的进程级崩溃淹没在噪音里。

现在改成原地轮询等待：配置一旦在页面上改好，进程自己就连上去，不需要人再去
重启它。**只有重启也救不了的静态错配**（TASK_EXEC_BACKEND=sync）才硬失败退出。
交互调试想要旧的快速失败语义，加 --no-wait。
"""
import os
import time

from django.core.management.base import BaseCommand, CommandError

# 等待期间重复打同一条原因的间隔（秒）。首次以及原因变化时立即打，
# 其余靠这个节流——否则 30s 一条，一夜就是几千行同样的话。
REASON_REPEAT_INTERVAL = 600


class Command(BaseCommand):
    help = '启动飞书长连接事件接收进程（常驻）'

    def add_arguments(self, parser):
        parser.add_argument('--force', action='store_true',
                            help='即使配置里选的是回调模式也启动（调试用）')
        parser.add_argument('--no-wait', action='store_true',
                            help='配置未就绪时直接退出，不原地等待（交互调试用）')
        parser.add_argument('--wait-interval', type=int, default=30,
                            metavar='SECONDS',
                            help='等待配置就绪时的轮询间隔，默认 30 秒')

    def handle(self, *args, **options):
        # SDK 在 asyncio 事件循环里同步调用 handler，Django ORM 在异步上下文中
        # 默认会抛 SynchronousOnlyOperation。这里明确放行——由此也带来一条硬约束：
        # 执行后端必须是 celery，sync 后端会把 ansible 跑在事件循环里卡死连接
        os.environ.setdefault('DJANGO_ALLOW_ASYNC_UNSAFE', 'true')

        from django.conf import settings

        from apps.im_mgr import ws

        # 静态错配先判：它来自 settings 而非数据库，重启多少次都是同一个结果，
        # 所以这里必须退出而不是等待。
        backend = getattr(settings, 'TASK_EXEC_BACKEND', 'thread')
        if backend == 'sync':
            raise CommandError(
                'TASK_EXEC_BACKEND=sync 与长连接不能共存：任务会在事件循环里同步'
                '执行，整条连接被卡死。生产请用 celery。')
        if backend != 'celery':
            self.stdout.write(self.style.WARNING(
                f'  [!!] TASK_EXEC_BACKEND={backend}，生产建议用 celery'))

        app = self._await_ready(options)
        if app is None:
            return

        self.stdout.write(f'飞书长连接启动：app_id={app.app_id}')
        try:
            ws.build_client(app).start()
        except KeyboardInterrupt:
            self.stdout.write('已停止。')

    # ── 配置就绪判定 ────────────────────────────────────────────────────────
    def _blocking_reason(self, app, force):
        """返回阻塞启动的原因（字符串），齐全则返回 None。"""
        if not app.is_ready():
            missing = app.missing_fields()
            return ('飞书接入未就绪：'
                    f'{"、".join(missing) if missing else "未启用"}')
        if not app.use_websocket and not force:
            return ('当前事件接收方式是「回调」。请到「系统管理 → 飞书接入」改成'
                    '「长连接」——两种模式同时开启会让同一个事件被处理两次。')
        return None

    def _await_ready(self, options):
        """轮询到配置就绪为止，返回可用的 FeishuApp；--no-wait 下不就绪即抛。

        收到 SIGINT（compose 的 stop_signal，supervisor 的 stopsignal）时
        返回 None，由 handle 正常收尾。
        """
        from django.db import close_old_connections

        from apps.im_mgr.models import FeishuApp

        force = options['force']
        interval = max(1, options['wait_interval'])
        last_reason = None
        last_logged_at = 0.0

        while True:
            # 等待可能持续数小时，中间连接会被 MySQL 的 wait_timeout 掐掉；
            # 不回收的话轮询会在某一轮突然抛 OperationalError 而进程退出，
            # 恰好又变回重启循环。
            close_old_connections()
            app = FeishuApp.load()
            reason = self._blocking_reason(app, force)
            if reason is None:
                if last_reason is not None:
                    self.stdout.write(self.style.SUCCESS('配置已就绪，开始连接。'))
                return app

            if options['no_wait']:
                raise CommandError(reason)

            now = time.monotonic()
            if reason != last_reason or now - last_logged_at >= REASON_REPEAT_INTERVAL:
                self.stdout.write(self.style.WARNING(
                    f'{reason} 进程原地等待，配置改好后会自动连接'
                    f'（每 {interval}s 复查一次）。'))
                last_logged_at = now
            last_reason = reason

            try:
                time.sleep(interval)
            except KeyboardInterrupt:
                self.stdout.write('已停止。')
                return None
