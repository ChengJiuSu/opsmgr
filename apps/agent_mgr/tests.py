"""agent_mgr 测试：管理 API 权限与 token、Consumer 协议、dispatch 与 runner 分组。"""
from unittest import mock

from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator

from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase, override_settings
from rest_framework.test import APIClient

from .dispatch import run_via_agent
from .models import AgentJob, AgentNode, hash_token
from .routing import websocket_urlpatterns

User = get_user_model()

IN_MEMORY_LAYER = {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}}


class BaseApiTest(TestCase):
    def _client(self, is_staff=True, username=None):
        username = username or ('admin_g' if is_staff else 'user_g')
        u = User.objects.create(username=username, is_staff=is_staff)
        u.set_password('Pass!2345xyz')
        u.save()
        c = APIClient()
        r = c.post('/api/auth/login/', {'username': username, 'password': 'Pass!2345xyz'}, format='json')
        c.credentials(HTTP_AUTHORIZATION='Bearer ' + r.json()['access'])
        return c


class AgentNodeApiTests(BaseApiTest):
    def test_create_returns_token_once(self):
        admin = self._client()
        r = admin.post('/api/agents/', {'name': 'nat-gw-01'}, format='json')
        self.assertEqual(r.status_code, 200)
        data = r.json()['data']
        token = data['token']
        self.assertTrue(token)
        node = AgentNode.objects.get(name='nat-gw-01')
        self.assertEqual(node.token_hash, hash_token(token))
        # 列表不回显 token / token_hash
        r = admin.get('/api/agents/')
        body = r.json()
        listed = body[0] if isinstance(body, list) else body['data'][0]
        self.assertNotIn('token', listed)
        self.assertNotIn('token_hash', listed)

    def test_permission_admin_only(self):
        normal = self._client(is_staff=False)
        self.assertEqual(normal.get('/api/agents/').status_code, 403)
        self.assertEqual(normal.post('/api/agents/', {'name': 'x'}, format='json').status_code, 403)

    def test_regen_token_invalidates_old(self):
        admin = self._client()
        node_id = admin.post('/api/agents/', {'name': 'gw'}, format='json').json()['data']['id']
        old_hash = AgentNode.objects.get(pk=node_id).token_hash
        r = admin.post(f'/api/agents/{node_id}/regen_token/')
        self.assertEqual(r.status_code, 200)
        node = AgentNode.objects.get(pk=node_id)
        self.assertNotEqual(node.token_hash, old_hash)
        self.assertEqual(node.token_hash, hash_token(r.json()['data']['token']))

    def test_delete_protected_when_jobs_active(self):
        admin = self._client()
        node_id = admin.post('/api/agents/', {'name': 'gw2'}, format='json').json()['data']['id']
        node = AgentNode.objects.get(pk=node_id)
        job = AgentJob.objects.create(agent=node, status='running')
        r = admin.delete(f'/api/agents/{node_id}/')
        self.assertEqual(r.status_code, 400)
        job.status = 'done'
        job.save(update_fields=['status'])
        r = admin.delete(f'/api/agents/{node_id}/')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(AgentNode.objects.filter(pk=node_id).exists())

    def test_jobs_action(self):
        admin = self._client()
        node_id = admin.post('/api/agents/', {'name': 'gw3'}, format='json').json()['data']['id']
        node = AgentNode.objects.get(pk=node_id)
        AgentJob.objects.create(agent=node, status='done', return_code=0)
        r = admin.get(f'/api/agents/{node_id}/jobs/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()['data']), 1)
        self.assertEqual(r.json()['data'][0]['status'], 'done')


@override_settings(CHANNEL_LAYERS=IN_MEMORY_LAYER)
class AgentConsumerTests(TransactionTestCase):
    """Consumer 协议测试：TransactionTestCase 保证 database_sync_to_async
    线程连接可见同一数据（TestCase 的事务隔离会挡住跨线程读写）。"""

    def setUp(self):
        raw, token_hash = AgentNode.generate_token()
        self.token = raw
        self.node = AgentNode.objects.create(name='gw-ws', token_hash=token_hash)
        self.app = URLRouter(websocket_urlpatterns)

    def _communicator(self, token=None):
        return WebsocketCommunicator(
            self.app, '/ws/agent/',
            headers=[(b'authorization', f'Token {token or self.token}'.encode())])

    async def _connect(self):
        comm = self._communicator()
        connected, _ = await comm.connect()
        assert connected
        return comm

    async def test_auth_reject_bad_token(self):
        comm = self._communicator(token='wrong-token')
        connected, _ = await comm.connect()
        # accept 后立即 close(4401)：表现为收到关闭帧
        if connected:
            msg = await comm.receive_output()
            self.assertEqual(msg['type'], 'websocket.close')
            self.assertEqual(msg['code'], 4401)
        await comm.disconnect()

    async def test_register_and_heartbeat(self):
        comm = await self._connect()
        await comm.send_json_to({'type': 'register', 'data': {
            'version': '1.0.0', 'hostname': 'natgw'}})
        reply = await comm.receive_json_from()
        self.assertEqual(reply['type'], 'register.ack')
        await comm.send_json_to({'type': 'heartbeat', 'data': {
            'running_jobs': 1, 'load1': 0.5, 'disk_free_pct': 80}})
        await comm.receive_nothing()
        node = await AgentNode.objects.aget(pk=self.node.pk)
        self.assertEqual(node.status, 'online')
        self.assertEqual(node.version, '1.0.0')
        self.assertEqual(node.meta.get('hostname'), 'natgw')
        self.assertEqual(node.meta.get('running_jobs'), 1)
        self.assertIsNotNone(node.last_heartbeat)
        await comm.disconnect()
        node = await AgentNode.objects.aget(pk=self.node.pk)
        self.assertEqual(node.status, 'offline')
        self.assertEqual(node.channel_name, '')

    async def test_log_chunk_append_and_idempotent(self):
        job = await AgentJob.objects.acreate(agent=self.node, status='running')
        comm = await self._connect()
        await comm.send_json_to({'type': 'log.chunk', 'data': {
            'job_id': str(job.job_id), 'chunk_seq': 1, 'lines': ['line1', 'line2']}})
        ack = await comm.receive_json_from()
        self.assertEqual(ack['type'], 'log.ack')
        self.assertEqual(ack['data']['chunk_seq'], 1)
        # 重复分片：不再追加，仅重 ack 当前游标
        await comm.send_json_to({'type': 'log.chunk', 'data': {
            'job_id': str(job.job_id), 'chunk_seq': 1, 'lines': ['line1', 'line2']}})
        ack = await comm.receive_json_from()
        self.assertEqual(ack['data']['chunk_seq'], 1)
        job = await AgentJob.objects.aget(pk=job.pk)
        self.assertEqual(job.output, 'line1\nline2\n')
        self.assertEqual(job.chunk_seq, 1)
        await comm.disconnect()

    async def test_task_result_finishes_job(self):
        job = await AgentJob.objects.acreate(agent=self.node, status='running')
        comm = await self._connect()
        await comm.send_json_to({'type': 'task.result', 'data': {
            'job_id': str(job.job_id), 'rc': 0, 'elapsed': 3.2,
            'host_results': {'game-01': 'ok'}}})
        ack = await comm.receive_json_from()
        self.assertEqual(ack['type'], 'result.ack')
        job = await AgentJob.objects.aget(pk=job.pk)
        self.assertEqual(job.status, 'done')
        self.assertEqual(job.return_code, 0)
        self.assertEqual(job.result['host_results'], {'game-01': 'ok'})
        await comm.disconnect()

    async def test_task_ack_rejected_marks_failed(self):
        job = await AgentJob.objects.acreate(agent=self.node, status='sent')
        comm = await self._connect()
        await comm.send_json_to({'type': 'task.ack', 'data': {
            'job_id': str(job.job_id), 'accepted': False, 'reason': '并发队列已满'}})
        await comm.receive_nothing()
        job = await AgentJob.objects.aget(pk=job.pk)
        self.assertEqual(job.status, 'failed')
        self.assertEqual(job.error, '并发队列已满')
        await comm.disconnect()

    async def test_dispatch_event_forwarded(self):
        comm = await self._connect()
        from channels.layers import get_channel_layer

        node = await AgentNode.objects.aget(pk=self.node.pk)
        await get_channel_layer().group_send(
            node.group_name, {'type': 'agent.dispatch', 'job': {'job_id': 'j1', 'step_type': 'command'}})
        msg = await comm.receive_json_from()
        self.assertEqual(msg['type'], 'task.dispatch')
        self.assertEqual(msg['data']['job_id'], 'j1')
        await comm.disconnect()


class DispatchTests(TestCase):
    def test_offline_agent_fails_fast(self):
        node = AgentNode.objects.create(name='gw-off', token_hash='x', status='offline')
        rc, out = run_via_agent(node, {'step_type': 'command'}, timeout=10)
        self.assertEqual(rc, 1)
        self.assertIn('离线', out)
        self.assertFalse(AgentJob.objects.exists())  # 离线不落 job 记录

    def test_disabled_agent_fails_fast(self):
        node = AgentNode.objects.create(
            name='gw-dis', token_hash='x', status='online', channel_name='ch', enabled=False)
        rc, out = run_via_agent(node, {'step_type': 'command'}, timeout=10)
        self.assertEqual(rc, 1)
        self.assertIn('已禁用', out)


class RunnerChannelSplitTests(TestCase):
    """runner 通道分组：直连组走本地执行，agent 组经 dispatch 下发。"""

    def setUp(self):
        from apps.asset_mgr.models import Server, ServerService, ServiceType

        self.agent = AgentNode.objects.create(
            name='gw-run', token_hash='x', status='online', channel_name='ch')
        self.direct = Server.objects.create(name='direct-01', private_ip='10.0.0.1')
        self.via_agent = Server.objects.create(
            name='nat-01', private_ip='192.168.1.10', agent=self.agent)
        svc = ServiceType.objects.create(name='游戏服', code='game-server')
        for srv in (self.direct, self.via_agent):
            ServerService.objects.create(server=srv, service_type=svc, status='planned')

    def _task_and_step(self):
        from apps.task_exec.models import ExecutionTask, TaskStep

        task = ExecutionTask.objects.create(name='t', exec_mode='parallel')
        task.servers.set([self.direct, self.via_agent])
        step = TaskStep.objects.create(
            task=task, order=1, name='s1', task_type='command',
            command='echo hi', timeout=60)
        return task, step

    def test_split_by_agent(self):
        from apps.task_exec.runner import _split_by_agent

        direct, groups = _split_by_agent([self.direct, self.via_agent])
        self.assertEqual(direct, [self.direct])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0][0], self.agent)
        self.assertEqual(groups[0][1], [self.via_agent])

    def test_agent_inventory_excludes_secrets(self):
        from apps.task_exec.models import Credential
        from apps.task_exec.runner import _inventory_content

        cred = Credential(name='c', auth_type='password', username='ops',
                          password='secret-pass', become=True, become_password='root-pass')
        content = _inventory_content([self.via_agent], cred, None, include_secrets=False)
        self.assertIn('ansible_user=ops', content)
        self.assertIn('ansible_become=true', content)
        self.assertNotIn('secret-pass', content)
        self.assertNotIn('root-pass', content)
        self.assertIn('ansible_host=192.168.1.10', content)

    def test_command_step_mixed_channels(self):
        from apps.task_exec import runner

        task, step = self._task_and_step()
        with mock.patch.object(runner, 'run_subprocess', return_value=(0, '直连输出')) as m_local, \
                mock.patch('apps.agent_mgr.dispatch.run_via_agent',
                           return_value=(0, 'agent 输出')) as m_agent:
            rc, out = runner._run_command_step(step, task, [self.direct, self.via_agent])
        self.assertEqual(rc, 0)
        self.assertIn('[直连 · 1 台]', out)
        self.assertIn(f'[经 Agent {self.agent.name} · 1 台]', out)
        self.assertIn('直连输出', out)
        self.assertIn('agent 输出', out)
        m_local.assert_called_once()
        m_agent.assert_called_once()
        payload = m_agent.call_args.args[1]
        self.assertEqual(payload['step_type'], 'command')
        self.assertEqual(payload['command'], 'echo hi')
        self.assertIn('ansible_host=192.168.1.10', payload['inventory'])

    def test_command_step_direct_only_keeps_plain_output(self):
        from apps.task_exec import runner

        task, step = self._task_and_step()
        with mock.patch.object(runner, 'run_subprocess', return_value=(0, '直连输出')):
            rc, out = runner._run_command_step(step, task, [self.direct])
        self.assertEqual(rc, 0)
        self.assertEqual(out, '直连输出')  # 无 agent 组时输出不加通道标头

    def test_agent_group_failure_propagates_rc(self):
        from apps.task_exec import runner

        task, step = self._task_and_step()
        with mock.patch.object(runner, 'run_subprocess', return_value=(0, 'ok')), \
                mock.patch('apps.agent_mgr.dispatch.run_via_agent', return_value=(2, '失败')):
            rc, out = runner._run_command_step(step, task, [self.direct, self.via_agent])
        self.assertEqual(rc, 2)
