"""ops-agent WebSocket 服务端（/ws/agent/）。

协议见 docs/ops-agent-design.md 第 3 章：JSON 文本帧
{"type": ..., "seq": ..., "ts": ..., "data": {...}}。

职责：token 鉴权、register/heartbeat 落库、runner 下发转发（channel layer
组消息）、log.chunk / task.result 幂等落库并回 ack、断线置离线。
同名 agent 重复连接时踢掉旧连接（防脑裂）。
"""
import logging

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from django.db.models import F, Value
from django.db.models.functions import Concat
from django.utils import timezone

from .models import AgentJob, AgentNode, hash_token

logger = logging.getLogger('apps.agent_mgr')

# 关闭码：4401 鉴权失败；4409 被新连接顶替
CLOSE_UNAUTHORIZED = 4401
CLOSE_REPLACED = 4409

# 单个 job 输出上限（字符）：超限截断并标注，防疯狂输出撑爆数据库
JOB_OUTPUT_LIMIT = 2 * 1024 * 1024


class AgentConsumer(AsyncJsonWebsocketConsumer):
    """每个 agent 一条连接；连接即代表该 AgentNode 在线。"""

    agent_id = None
    agent_name = ''

    # ── 连接生命周期 ──────────────────────────────────────

    async def connect(self):
        token = self._bearer_token()
        agent = await self._auth(token) if token else None
        if agent is None:
            await self.accept()
            await self.close(code=CLOSE_UNAUTHORIZED)
            return
        self.agent_id = agent['id']
        self.agent_name = agent['name']
        await self.accept()
        old_channel = await self._bind_channel()
        if old_channel:
            # 踢旧连接：直接发关闭事件给旧 channel，防止双连接脑裂
            await self.channel_layer.send(old_channel, {'type': 'agent.kick'})
        await self.channel_layer.group_add(self._group, self.channel_name)
        logger.info('agent %s 已连接', self.agent_name)

    async def disconnect(self, code):
        if self.agent_id is None:
            return
        await self.channel_layer.group_discard(self._group, self.channel_name)
        went_offline = await self._release_channel()
        if went_offline:
            logger.warning('agent %s 断开连接（code=%s），已置离线', self.agent_name, code)

    # ── agent 上行消息 ────────────────────────────────────

    async def receive_json(self, content, **kwargs):
        msg_type = content.get('type')
        data = content.get('data') or {}
        handler = {
            'register': self._on_register,
            'heartbeat': self._on_heartbeat,
            'task.ack': self._on_task_ack,
            'log.chunk': self._on_log_chunk,
            'task.result': self._on_task_result,
        }.get(msg_type)
        if handler is None:
            await self.send_json({'type': 'error', 'data': {
                'code': 'unknown_type', 'message': f'未知消息类型：{msg_type}'}})
            return
        try:
            await handler(data)
        except Exception:
            logger.exception('agent %s 消息处理失败：type=%s', self.agent_name, msg_type)
            await self.send_json({'type': 'error', 'data': {
                'code': 'internal', 'message': f'消息处理失败：{msg_type}'}})

    async def _on_register(self, data):
        await self._update_node(
            version=str(data.get('version') or '')[:32],
            meta_patch={'hostname': data.get('hostname', '')},
        )
        await self.send_json({'type': 'register.ack', 'data': {'name': self.agent_name}})

    async def _on_heartbeat(self, data):
        await self._update_node(
            version=str(data.get('version') or '')[:32] or None,
            meta_patch={
                'running_jobs': data.get('running_jobs'),
                'load1': data.get('load1'),
                'disk_free_pct': data.get('disk_free_pct'),
            },
        )

    async def _on_task_ack(self, data):
        job_id = data.get('job_id')
        if data.get('accepted'):
            await self._job_update(job_id, from_statuses=('pending', 'sent'), status='running')
        else:
            reason = str(data.get('reason') or 'agent 拒收')[:500]
            await self._job_update(
                job_id, from_statuses=('pending', 'sent'),
                status='failed', return_code=1, error=reason)

    async def _on_log_chunk(self, data):
        job_id = data.get('job_id')
        chunk_seq = int(data.get('chunk_seq') or 0)
        lines = data.get('lines') or []
        acked = await self._append_chunk(job_id, chunk_seq, lines)
        if acked is not None:
            await self.send_json({'type': 'log.ack', 'data': {
                'job_id': job_id, 'chunk_seq': acked}})

    async def _on_task_result(self, data):
        job_id = data.get('job_id')
        await self._finish_job(
            job_id,
            return_code=int(data.get('rc') if data.get('rc') is not None else 1),
            result={'host_results': data.get('host_results') or {},
                    'elapsed': data.get('elapsed')},
        )
        # 无论是否重复上报都回 ack：agent 收到 ack 才清理本地 spool
        await self.send_json({'type': 'result.ack', 'data': {'job_id': job_id}})

    # ── channel layer 下行事件（runner / 管理端触发）──────

    async def agent_dispatch(self, event):
        """runner 下发模块执行单元。"""
        await self.send_json({'type': 'task.dispatch', 'data': event['job']})

    async def agent_cancel(self, event):
        """转发取消指令（模块超时/任务中止）。"""
        await self.send_json({'type': 'task.cancel', 'data': {'job_id': event['job_id']}})

    async def agent_kick(self, event):
        """被同名 agent 的新连接顶替。"""
        await self.close(code=CLOSE_REPLACED)

    # ── 私有工具 ──────────────────────────────────────────

    @property
    def _group(self):
        return f'agent_{self.agent_id}'

    def _bearer_token(self):
        """从握手头解析 Authorization: Token <raw>。"""
        for key, value in self.scope.get('headers') or []:
            if key == b'authorization':
                parts = value.decode('utf-8', 'ignore').split()
                if len(parts) == 2 and parts[0].lower() == 'token':
                    return parts[1]
        return None

    @database_sync_to_async
    def _auth(self, token):
        node = AgentNode.objects.filter(token_hash=hash_token(token), enabled=True).first()
        if node is None:
            return None
        return {'id': node.id, 'name': node.name}

    @database_sync_to_async
    def _bind_channel(self):
        """登记本连接并置在线，返回被顶替的旧 channel_name（无则 None）。"""
        node = AgentNode.objects.filter(pk=self.agent_id).first()
        if node is None:
            return None
        old = node.channel_name or None
        AgentNode.objects.filter(pk=self.agent_id).update(
            channel_name=self.channel_name, status='online',
            last_heartbeat=timezone.now(), updated_at=timezone.now())
        return old if old != self.channel_name else None

    @database_sync_to_async
    def _release_channel(self):
        """断线清理：仅当前连接仍是登记连接时置离线（被踢的旧连接不动新状态）。"""
        updated = AgentNode.objects.filter(
            pk=self.agent_id, channel_name=self.channel_name,
        ).update(channel_name='', status='offline', updated_at=timezone.now())
        return bool(updated)

    @database_sync_to_async
    def _update_node(self, version=None, meta_patch=None):
        node = AgentNode.objects.filter(pk=self.agent_id).first()
        if node is None:
            return
        fields = {'status': 'online', 'last_heartbeat': timezone.now(),
                  'updated_at': timezone.now()}
        if version:
            fields['version'] = version
        if meta_patch:
            meta = dict(node.meta or {})
            meta.update({k: v for k, v in meta_patch.items() if v is not None})
            fields['meta'] = meta
        AgentNode.objects.filter(pk=self.agent_id).update(**fields)

    @database_sync_to_async
    def _job_update(self, job_id, from_statuses, **fields):
        AgentJob.objects.filter(
            job_id=job_id, agent_id=self.agent_id, status__in=from_statuses,
        ).update(updated_at=timezone.now(), **fields)

    @database_sync_to_async
    def _append_chunk(self, job_id, chunk_seq, lines):
        """幂等追加日志分片，返回应回 ack 的游标（None = job 不存在）。

        chunk_seq 必须严格大于已入库游标才追加；重复/乱序分片直接重 ack
        当前游标，由 agent 端按游标补传。
        """
        job = AgentJob.objects.filter(job_id=job_id, agent_id=self.agent_id).first()
        if job is None:
            return None
        if chunk_seq <= job.chunk_seq or job.status in AgentJob.TERMINAL_STATUSES:
            return job.chunk_seq
        text = '\n'.join(str(line) for line in lines)
        if text and len(job.output) < JOB_OUTPUT_LIMIT:
            if len(job.output) + len(text) > JOB_OUTPUT_LIMIT:
                text = text[:JOB_OUTPUT_LIMIT - len(job.output)] + '\n…（输出超限已截断）'
            AgentJob.objects.filter(pk=job.pk).update(
                output=Concat(F('output'), Value(text + '\n')),
                chunk_seq=chunk_seq, updated_at=timezone.now())
        else:
            AgentJob.objects.filter(pk=job.pk).update(
                chunk_seq=chunk_seq, updated_at=timezone.now())
        return chunk_seq

    @database_sync_to_async
    def _finish_job(self, job_id, return_code, result):
        AgentJob.objects.filter(
            job_id=job_id, agent_id=self.agent_id,
        ).exclude(status__in=AgentJob.TERMINAL_STATUSES).update(
            status='done', return_code=return_code, result=result,
            updated_at=timezone.now())
