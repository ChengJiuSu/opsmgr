"""agent_mgr 测试：管理 API 权限与 token、Consumer 协议、dispatch、通道解析与 runner 分组。"""
import importlib
from unittest import mock

from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.db.utils import IntegrityError
from django.test import TestCase, TransactionTestCase, override_settings
from rest_framework.test import APIClient

from .channel import resolve_channel, resolve_channels
from .dispatch import run_via_agent
from .models import AgentJob, AgentNode, AgentRoute, hash_token
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
            name='nat-01', private_ip='192.168.1.10', channel_mode='agent', agent=self.agent)
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

    def test_split_by_agent_via_route_rule(self):
        """主机没有任何 agent 配置，靠项目组路由规则进 agent 组。"""
        from apps.asset_mgr.models import Environment, ProjectGroup, Server
        from apps.task_exec.runner import _split_by_agent

        pg = ProjectGroup.objects.create(name='分组项目', code='split-p1')
        env = Environment.objects.create(name='分组正式', code='split-prod')
        ruled = Server.objects.create(
            name='ruled-01', private_ip='192.168.2.10', project_group=pg, environment=env)
        AgentRoute.objects.create(agent=self.agent, project_group=pg)

        direct, groups = _split_by_agent([self.direct, ruled])
        self.assertEqual(direct, [self.direct])
        self.assertEqual(groups[0][1], [ruled])

    def test_split_by_agent_force_direct_overrides_rule(self):
        from apps.asset_mgr.models import Environment, ProjectGroup, Server
        from apps.task_exec.runner import _split_by_agent

        pg = ProjectGroup.objects.create(name='强直项目', code='split-p2')
        Environment.objects.create(name='强直正式', code='split-prod2')
        pinned_direct = Server.objects.create(
            name='mgr-01', private_ip='192.168.2.20', project_group=pg, channel_mode='direct')
        AgentRoute.objects.create(agent=self.agent, project_group=pg)

        direct, groups = _split_by_agent([pinned_direct])
        self.assertEqual(direct, [pinned_direct])
        self.assertEqual(groups, [])

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
        with mock.patch.object(runner, 'run_stream', return_value=(0, '直连输出')) as m_local, \
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
        with mock.patch.object(runner, 'run_stream', return_value=(0, '直连输出')):
            rc, out = runner._run_command_step(step, task, [self.direct])
        self.assertEqual(rc, 0)
        self.assertEqual(out, '直连输出')  # 无 agent 组时输出不加通道标头

    def test_agent_group_failure_propagates_rc(self):
        from apps.task_exec import runner

        task, step = self._task_and_step()
        with mock.patch.object(runner, 'run_stream', return_value=(0, 'ok')), \
                mock.patch('apps.agent_mgr.dispatch.run_via_agent', return_value=(2, '失败')):
            rc, out = runner._run_command_step(step, task, [self.direct, self.via_agent])
        self.assertEqual(rc, 2)


class ChannelLayerConfigTests(TestCase):
    """Redis 层连接参数：锁死 socket_timeout 与阻塞读时长的关系。

    redis-py 8.0 把 socket_timeout 默认值由 None 改成 5s，与 channels_redis 的
    BZPOPMIN 阻塞时长（brpop_timeout，5s）相等，导致客户端读超时必然先触发，
    daphne 每 5s 断开 agent 连接。二者关系必须由配置显式保证。
    """

    def test_socket_timeout_exceeds_brpop_timeout(self):
        from channels_redis.core import RedisChannelLayer
        from channels_redis.utils import create_pool, decode_hosts

        from ops_manager.settings.base import channel_layers_for

        config = channel_layers_for('redis')['default']['CONFIG']
        kwargs = create_pool(decode_hosts(config['hosts'])[0]).connection_kwargs
        self.assertGreater(kwargs['socket_timeout'], RedisChannelLayer.brpop_timeout)

    def test_memory_layer_needs_no_connection_config(self):
        from ops_manager.settings.base import channel_layers_for

        layers = channel_layers_for('memory')
        self.assertNotIn('CONFIG', layers['default'])


class AgentRouteApiTests(BaseApiTest):
    """通道路由 API：仅管理员、唯一冲突转中文报错、影响面预览。"""

    def setUp(self):
        from apps.asset_mgr.models import Environment, ProjectGroup

        self.admin = self._client()
        self.agent = AgentNode.objects.create(name='api-gw', token_hash='x')
        self.pg = ProjectGroup.objects.create(name='路由项目', code='route-p1')
        self.env = Environment.objects.create(name='路由正式', code='route-prod')

    def _create(self, **kwargs):
        payload = {'agent': self.agent.id, 'project_group': self.pg.id, **kwargs}
        return self.admin.post('/api/agent-routes/', payload, format='json')

    def test_crud(self):
        r = self._create(environment=self.env.id, remark='上海 NAT')
        self.assertEqual(r.status_code, 201)
        route_id = r.json()['id']
        r = self.admin.get('/api/agent-routes/')
        rows = r.json()
        self.assertEqual(rows[0]['agent_name'], 'api-gw')
        self.assertEqual(rows[0]['environment_name'], '路由正式')
        self.assertEqual(self.admin.patch(
            f'/api/agent-routes/{route_id}/', {'enabled': False}, format='json').status_code, 200)
        self.assertEqual(self.admin.delete(f'/api/agent-routes/{route_id}/').status_code, 204)

    def test_duplicate_rejected_with_message(self):
        self._create(environment=self.env.id)
        r = self._create(environment=self.env.id)
        self.assertEqual(r.status_code, 400)
        self.assertIn('已存在通道路由规则', str(r.json()))

    def test_duplicate_project_default_rejected(self):
        self._create()
        r = self._create()
        self.assertEqual(r.status_code, 400)
        self.assertIn('不限环境', str(r.json()))

    def test_admin_only(self):
        normal = self._client(is_staff=False, username='route_user')
        self.assertEqual(normal.get('/api/agent-routes/').status_code, 403)

    def test_preview_counts_only_auto_hosts(self):
        from apps.asset_mgr.models import Server

        Server.objects.create(name='r-01', private_ip='10.7.0.1',
                              project_group=self.pg, environment=self.env)
        Server.objects.create(name='r-02', private_ip='10.7.0.2', project_group=self.pg,
                              environment=self.env, channel_mode='direct')
        r = self.admin.get(f'/api/agent-routes/preview/?project_group={self.pg.id}')
        data = r.json()['data']
        self.assertEqual(data['count'], 1)  # 强制直连的主机不受规则影响
        self.assertEqual(data['hosts'][0]['name'], 'r-01')

    def test_preview_requires_project_group(self):
        self.assertEqual(self.admin.get('/api/agent-routes/preview/').status_code, 400)


class ChannelResolveTests(TestCase):
    """执行通道解析：主机三态优先于路由规则，规则内精确环境优先于项目组默认。"""

    def setUp(self):
        from apps.asset_mgr.models import Environment, ProjectGroup, Server

        self.agent_a = AgentNode.objects.create(name='gw-a', token_hash='x')
        self.agent_b = AgentNode.objects.create(name='gw-b', token_hash='y')
        # 字典项由种子迁移预置，测试用独立编码避免撞唯一约束
        self.pg = ProjectGroup.objects.create(name='通道项目一', code='ch-p1')
        self.other_pg = ProjectGroup.objects.create(name='通道项目二', code='ch-p2')
        self.env_test = Environment.objects.create(name='通道测试', code='ch-test')
        self.env_prod = Environment.objects.create(name='通道正式', code='ch-prod')
        self.server = Server.objects.create(
            name='nat-01', private_ip='192.168.1.10',
            project_group=self.pg, environment=self.env_prod)

    def _route(self, agent=None, environment=None, enabled=True, project_group=None):
        return AgentRoute.objects.create(
            agent=agent or self.agent_a, project_group=project_group or self.pg,
            environment=environment, enabled=enabled)

    def test_no_rule_is_direct(self):
        self.assertEqual(resolve_channel(self.server), (None, 'direct'))

    def test_project_default_rule(self):
        self._route()
        self.assertEqual(resolve_channel(self.server), (self.agent_a, 'rule'))

    def test_exact_env_rule_beats_project_default(self):
        self._route(agent=self.agent_a)                            # 项目组默认
        self._route(agent=self.agent_b, environment=self.env_prod)  # 精确环境
        self.assertEqual(resolve_channel(self.server), (self.agent_b, 'rule'))

    def test_other_env_rule_falls_back_to_default(self):
        self._route(agent=self.agent_a)
        self._route(agent=self.agent_b, environment=self.env_test)
        self.assertEqual(resolve_channel(self.server), (self.agent_a, 'rule'))

    def test_other_project_rule_does_not_leak(self):
        self._route(agent=self.agent_b, project_group=self.other_pg)
        self.assertEqual(resolve_channel(self.server), (None, 'direct'))

    def test_disabled_rule_ignored(self):
        self._route(enabled=False)
        self.assertEqual(resolve_channel(self.server), (None, 'direct'))

    def test_host_pin_wins_over_rule(self):
        self._route(agent=self.agent_a)
        self.server.channel_mode = 'agent'
        self.server.agent = self.agent_b
        self.server.save()
        self.assertEqual(resolve_channel(self.server), (self.agent_b, 'host'))

    def test_force_direct_ignores_rule(self):
        self._route(agent=self.agent_a)
        self.server.channel_mode = 'direct'
        self.server.save()
        self.assertEqual(resolve_channel(self.server), (None, 'direct'))

    def test_pinned_agent_deleted_falls_back_to_rule(self):
        """agent 删除后 FK 置空，mode 仍是 agent：按 auto 处理，不做僵尸配置。"""
        self._route(agent=self.agent_a)
        self.server.channel_mode = 'agent'
        self.server.agent = self.agent_b
        self.server.save()
        self.agent_b.delete()
        self.server.refresh_from_db()
        self.assertEqual(resolve_channel(self.server), (self.agent_a, 'rule'))

    def test_rule_agent_disabled_still_routed(self):
        """agent 禁用只影响建连，不改变路由——静默改走直连只会换来 SSH 超时。"""
        self.agent_a.enabled = False
        self.agent_a.save()
        self._route(agent=self.agent_a)
        self.assertEqual(resolve_channel(self.server), (self.agent_a, 'rule'))

    def test_host_without_project_group_is_direct(self):
        from apps.asset_mgr.models import Server

        self._route()
        orphan = Server.objects.create(name='no-pg', private_ip='10.0.0.9')
        self.assertEqual(resolve_channel(orphan), (None, 'direct'))

    def test_batch_resolve_query_count(self):
        """批量解析固定 2 次查询（规则 + 钉死 agent），不随主机数增长。"""
        from apps.asset_mgr.models import Server

        self._route()
        for i in range(5):
            Server.objects.create(
                name=f'h{i}', private_ip=f'10.1.0.{i}', project_group=self.pg,
                environment=self.env_prod, channel_mode='agent', agent=self.agent_b)
        # 从库里重取（无 select_related）：钉死的 agent 也只补一次 in_bulk
        servers = list(Server.objects.all())
        with self.assertNumQueries(2):
            resolved = resolve_channels(servers)
        self.assertEqual(resolved[self.server.pk], (self.agent_a, 'rule'))
        pinned = Server.objects.get(name='h0')
        self.assertEqual(resolved[pinned.pk], (self.agent_b, 'host'))


class AgentRouteConstraintTests(TestCase):
    """规则唯一性：同项目组同环境唯一，项目组默认（环境为空）也唯一。"""

    def setUp(self):
        from apps.asset_mgr.models import Environment, ProjectGroup

        self.agent = AgentNode.objects.create(name='gw-c', token_hash='x')
        self.pg = ProjectGroup.objects.create(name='约束项目', code='ch-c1')
        self.env = Environment.objects.create(name='约束正式', code='ch-c-prod')

    def test_duplicate_exact_rule_rejected(self):
        AgentRoute.objects.create(agent=self.agent, project_group=self.pg, environment=self.env)
        with self.assertRaises(IntegrityError):
            AgentRoute.objects.create(
                agent=self.agent, project_group=self.pg, environment=self.env)

    def test_duplicate_project_default_rejected(self):
        """环境为空的默认规则：SQL 中 NULL != NULL，靠带条件的单字段约束兜住。"""
        AgentRoute.objects.create(agent=self.agent, project_group=self.pg)
        with self.assertRaises(IntegrityError):
            AgentRoute.objects.create(agent=self.agent, project_group=self.pg)

    def test_default_and_exact_coexist(self):
        AgentRoute.objects.create(agent=self.agent, project_group=self.pg)
        AgentRoute.objects.create(agent=self.agent, project_group=self.pg, environment=self.env)
        self.assertEqual(AgentRoute.objects.count(), 2)

    def test_deleting_agent_removes_its_routes(self):
        AgentRoute.objects.create(agent=self.agent, project_group=self.pg)
        self.agent.delete()
        self.assertEqual(AgentRoute.objects.count(), 0)


class ChannelModeMigrationTests(TestCase):
    """0010 数据迁移：存量绑定 agent 的主机必须被钉成 mode=agent。"""

    def _migration(self):
        return importlib.import_module('apps.asset_mgr.migrations.0010_server_channel_mode')

    def test_existing_agent_hosts_pinned(self):
        from apps.asset_mgr.models import Server

        agent = AgentNode.objects.create(name='gw-m', token_hash='x')
        pinned = Server.objects.create(name='old-nat', private_ip='192.168.9.1', agent=agent)
        plain = Server.objects.create(name='old-direct', private_ip='10.9.0.1')
        # 迁移前的库态：字段已加、值仍是默认 auto
        Server.objects.update(channel_mode='auto')

        self._migration().pin_existing_agent_hosts(django_apps, None)

        pinned.refresh_from_db()
        plain.refresh_from_db()
        self.assertEqual(pinned.channel_mode, 'agent')
        self.assertEqual(plain.channel_mode, 'auto')

    def test_reverse_clears_agent_on_non_agent_hosts(self):
        from apps.asset_mgr.models import Server

        agent = AgentNode.objects.create(name='gw-m2', token_hash='x')
        auto_host = Server.objects.create(
            name='auto-h', private_ip='10.9.0.2', agent=agent, channel_mode='auto')
        kept = Server.objects.create(
            name='pinned-h', private_ip='10.9.0.3', agent=agent, channel_mode='agent')

        self._migration().unpin(django_apps, None)

        auto_host.refresh_from_db()
        kept.refresh_from_db()
        self.assertIsNone(auto_host.agent_id)
        self.assertEqual(kept.agent_id, agent.pk)
