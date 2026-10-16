"""任务执行日志的 WebSocket 推送（/ws/task-logs/<task_id>/）。

替代前端轮询：连接后先下发一次输出快照，随后由 live.LiveSink 在执行过程中
把新增分片经 channel layer 组消息推过来（组名 task_log_<task_id>）。

鉴权：浏览器 WebSocket 不能自定义请求头，token 通过 **子协议**传递
（客户端 `new WebSocket(url, ['jwt', <access_token>])`，token 不进 URL 与
访问日志）；兼容 `?token=` 查询参数。任务详情为登录可读，故校验到有效
用户即可放行。

推送消息（服务端 → 客户端）：
  {"kind": "full",   "output": <尾段正文>, "start": n, "offset": n,
                     "head": <与尾段不相连的前段，仅快照帧可能有>}
  {"kind": "delta",  "output": <新增片段>, "offset": n}
  {"kind": "steps",  "steps": [<逐模块结果，含未执行的 pending>]}
  {"kind": "status", "status": ..., "pause_type": ..., "return_code": ..., "duration": ...}

full 帧的 output 覆盖完整输出的绝对区间 [start, offset)：客户端据此把它盖回自己
那份更长的日志（只换窗口内的尾段），而不是整份替换成一个实时窗口——后者会让先前
模块的日志在执行中途消失。
"""
import logging
from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

logger = logging.getLogger('apps.task_exec')

# 关闭码：4401 鉴权失败；4404 任务不存在；4503 channel layer 不可用（前端回退轮询）
CLOSE_UNAUTHORIZED = 4401
CLOSE_NOT_FOUND = 4404
CLOSE_LAYER_UNAVAILABLE = 4503

# 子协议约定：客户端提供 ['jwt', <token>]，服务端 accept 'jwt'
TOKEN_SUBPROTOCOL = 'jwt'


def group_name(task_id):
    return f'task_log_{task_id}'


class TaskLogConsumer(AsyncJsonWebsocketConsumer):
    """一个执行日志面板一条连接（只下行，客户端无需上行消息）。"""

    task_id = None

    async def connect(self):
        try:
            self.task_id = int(self.scope['url_route']['kwargs']['task_id'])
        except (KeyError, TypeError, ValueError):
            await self.accept()
            await self.close(code=CLOSE_NOT_FOUND)
            return

        subprotocol = TOKEN_SUBPROTOCOL if TOKEN_SUBPROTOCOL in (
            self.scope.get('subprotocols') or []) else None
        if not await self._authenticate():
            await self.accept(subprotocol=subprotocol)
            await self.close(code=CLOSE_UNAUTHORIZED)
            return

        snapshot = await self._snapshot()
        if snapshot is None:
            await self.accept(subprotocol=subprotocol)
            await self.close(code=CLOSE_NOT_FOUND)
            return

        # 先入组（顺带探测 channel layer 是否真的可用：未配/连不上 Redis 直接拒
        # 握手，前端据此回退轮询，避免「连上了但永远收不到推送」）
        try:
            await self.channel_layer.group_add(group_name(self.task_id), self.channel_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning('任务日志推送通道不可用（任务 %s）：%s', self.task_id, exc)
            await self.close(code=CLOSE_LAYER_UNAVAILABLE)
            return

        await self.accept(subprotocol=subprotocol)
        # 先发快照（数据库内尾部预览），后续增量由 sink 推送
        await self.send_json({'kind': 'full', **snapshot})

    async def disconnect(self, code):
        if self.task_id is not None and self.channel_layer is not None:
            await self.channel_layer.group_discard(group_name(self.task_id), self.channel_name)

    async def receive_json(self, content, **kwargs):
        # 仅支持心跳（前端可发 {"type":"ping"} 保活长时间静默的连接）
        if (content or {}).get('type') == 'ping':
            await self.send_json({'kind': 'pong'})

    # ── channel layer 下行事件（LiveSink 触发）────────────
    async def task_log(self, event):
        await self.send_json(event['payload'])

    # ── 私有工具 ──────────────────────────────────────────
    def _token(self):
        """取 token：优先子协议（不进访问日志），兼容 ?token= 查询参数。"""
        subprotocols = [p for p in (self.scope.get('subprotocols') or [])
                        if p != TOKEN_SUBPROTOCOL]
        if subprotocols:
            return subprotocols[0]
        query = parse_qs((self.scope.get('query_string') or b'').decode('utf-8', 'ignore'))
        return (query.get('token') or [''])[0]

    @database_sync_to_async
    def _authenticate(self):
        """校验 JWT access token，返回是否为有效且启用的用户。"""
        from rest_framework_simplejwt.exceptions import TokenError
        from rest_framework_simplejwt.tokens import AccessToken

        from django.contrib.auth import get_user_model

        token = self._token()
        if not token:
            return False
        try:
            payload = AccessToken(token)
        except TokenError:
            return False
        user_id = payload.get('user_id')
        if not user_id:
            return False
        return get_user_model().objects.filter(pk=user_id, is_active=True).exists()

    @database_sync_to_async
    def _snapshot(self):
        """当前输出快照与状态（任务不存在返回 None）。

        执行中的快照不能只给库内尾部预览：切到别的执行记录再切回来会重连本通道，
        那时预览里往往只剩当前模块，先前模块的日志就「不见了」。live.read_live_output
        以落盘文件补回已完成模块的完整输出，并给出绝对偏移供后续增量落位。
        """
        from .live import read_live_output, resume_deadline_of
        from .models import ExecutionTask

        task = ExecutionTask.objects.filter(pk=self.task_id).first()
        if task is None:
            return None
        live = read_live_output(task)
        return {
            **live,
            'steps': self._latest_steps(task),
            'status': task.status,
            'pause_type': task.pause_type,
            'resume_deadline': resume_deadline_of(task),
            'return_code': task.return_code,
            'duration': task.duration,
        }

    @staticmethod
    def _latest_steps(task):
        """最新一次执行的模块表（执行中即含 pending 的完整任务流）。"""
        return (task.logs.order_by('-created_at', '-id')
                .values_list('steps_result', flat=True).first() or [])
