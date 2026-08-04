"""WebSocket 路由：任务执行日志实时推送。"""
from django.urls import path

from .consumers import TaskLogConsumer

websocket_urlpatterns = [
    path('ws/task-logs/<int:task_id>/', TaskLogConsumer.as_asgi()),
]
