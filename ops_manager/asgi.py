"""ASGI 入口：HTTP 走 Django 原生，/ws/ 走 Channels（ops-agent 接入）。

生产由 daphne 承载本应用（nginx 按 /ws/ 分流，HTTP 仍走 WSGI 现状）；
开发 runserver 因 INSTALLED_APPS 引入 daphne 自动切换为 ASGI，同端口可测。
"""
import os

from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ops_manager.settings.prod')

# 先初始化 Django（get_asgi_application 触发 setup），再导入依赖 ORM 的路由
django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402

from apps.agent_mgr.routing import websocket_urlpatterns  # noqa: E402

application = ProtocolTypeRouter({
    'http': django_asgi_app,
    'websocket': URLRouter(websocket_urlpatterns),
})
