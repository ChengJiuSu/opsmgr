"""平台方法执行器测试（Jenkins 直连 requests 全程 mock，经 Agent 路径 mock 下发）。"""
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase

from apps.system_cfg.models import SystemConfig

from .executors import get_executor
from .models import Method

BASE = 'http://jenkins.local:8080'
QUEUE_URL = f'{BASE}/queue/item/7/'
BUILD_URL = f'{BASE}/job/build-app/12/'


class FakeResp:
    def __init__(self, status=200, text='', headers=None, json_data=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}
        self._json = json_data or {}

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._json


def _step(params=None, timeout=60, project_group_id=None, environment_id=None,
          use_custom_scope=False, order=1, task_id=1):
    """执行器只读取 TaskStep 的少量字段，用简单对象即可。

    job_name 由模块参数指定，默认给出以覆盖主流程用例；project_group/environment
    经 step.task 提供（通道解析按任务维度回落 AgentRoute 规则）。
    """
    return SimpleNamespace(
        params={'job_name': 'build-app'} if params is None else params,
        timeout=timeout, order=order, task_id=task_id,
        use_custom_scope=use_custom_scope,
        project_group_id=project_group_id, environment_id=environment_id,
        task=SimpleNamespace(project_group_id=project_group_id,
                             environment_id=environment_id))


class JenkinsExecutorTests(TestCase):
    def setUp(self):
        SystemConfig.objects.create(key='jenkins.url', value=BASE)
        SystemConfig.objects.create(key='jenkins.user', value='ops')
        SystemConfig.objects.create(key='jenkins.api_token', value='token-xyz')
        # 方法为公共能力，不绑定 job：job_name 由任务模块 params 指定
        self.method = Method(
            name='构建', code='build-app', provider='jenkins',
            config={'default_params': {'BRANCH': 'master', 'KEEP': '1'}},
        )
        self.executor = get_executor('jenkins')

    def _session(self, result='SUCCESS', trigger_status=201):
        """构造覆盖 触发→队列→增量日志→结果 全流程的 mock Session。"""
        session = mock.MagicMock()
        session.post.return_value = FakeResp(
            status=trigger_status, headers={'Location': QUEUE_URL})
        queue_pending = FakeResp(json_data={'executable': None})
        queue_done = FakeResp(json_data={'executable': {'url': BUILD_URL, 'number': 12}})
        console_1 = FakeResp(text='clone code\n', headers={'X-Text-Size': '11', 'X-More-Data': 'true'})
        console_2 = FakeResp(text='build done\n', headers={'X-Text-Size': '22'})
        build_info = FakeResp(json_data={'result': result})

        def _get(url, **kwargs):
            if url.startswith(QUEUE_URL):
                return queue_pending if not session._queue_polled else queue_done
            if 'progressiveText' in url:
                session._console_calls += 1
                return console_1 if session._console_calls == 1 else console_2
            return build_info

        def _get_with_state(url, **kwargs):
            resp = _get(url, **kwargs)
            if url.startswith(QUEUE_URL):
                session._queue_polled = True
            return resp

        session._queue_polled = False
        session._console_calls = 0
        session.get.side_effect = _get_with_state
        return session

    def _execute(self, session, step=None, method=None):
        with mock.patch('apps.method_mgr.executors.jenkins.requests.Session', return_value=session), \
                mock.patch('apps.method_mgr.executors.jenkins.time.sleep'):
            return self.executor.execute(step or _step(), method or self.method)

    def test_success_flow_streams_console_log(self):
        session = self._session()
        rc, output = self._execute(session, step=_step(params={
            'job_name': 'build-app', 'BRANCH': 'release', 'artifact_version': '1.0.0',
        }))
        self.assertEqual(rc, 0)
        self.assertIn('[jenkins] job=build-app build=#12', output)
        self.assertIn('clone code', output)
        self.assertIn('build done', output)
        self.assertIn('构建结果：SUCCESS', output)
        # 构建参数：模块 params 覆盖方法默认值，平台保留键 job_name/artifact_version 不透传
        _, kwargs = session.post.call_args
        self.assertEqual(kwargs['params'], {'BRANCH': 'release', 'KEEP': '1'})
        args, _ = session.post.call_args
        self.assertEqual(args[0], f'{BASE}/job/build-app/buildWithParameters')

    def test_console_note_annotations_stripped(self):
        """Jenkins ConsoleNote 注解（\\x1b[8mha:<base64>\\x1b[0m）应从日志中剥除。"""
        session = self._session()
        noisy = ('\x1b[8mha:////4KQ/A3tkXrmJjdpGoVZnjv1EdD71LRtv+xyUn6P9Ves4AAAAlx+L==\x1b[0m'
                 'Started by user admin\n'
                 'ha:////4GTp8LxWlrNTCUBWOjBi6dxkveeKqzgnlYK3cThQPkMVAAAAoh+L==[Pipeline] node\n')

        def _get(url, **kwargs):
            if url.startswith(QUEUE_URL):
                return FakeResp(json_data={'executable': {'url': BUILD_URL, 'number': 12}})
            if 'progressiveText' in url:
                return FakeResp(text=noisy, headers={'X-Text-Size': str(len(noisy))})
            return FakeResp(json_data={'result': 'SUCCESS'})

        session.get.side_effect = _get
        rc, output = self._execute(session)
        self.assertEqual(rc, 0)
        self.assertNotIn('ha:////', output)
        self.assertNotIn('\x1b[', output)
        self.assertIn('Started by user admin', output)
        self.assertIn('[Pipeline] node', output)

    def test_missing_job_name(self):
        rc, output = self._execute(self._session(), step=_step(params={'BRANCH': 'x'}))
        self.assertEqual(rc, 1)
        self.assertIn('未指定 Jenkins Job', output)

    def test_config_job_name_as_fallback(self):
        """兼容存量：方法 config 里的 job_name 仍可作兜底默认值。"""
        method = Method(name='m', code='m', provider='jenkins', config={'job_name': 'legacy-job'})
        session = self._session()
        rc, _ = self._execute(session, step=_step(params={'BRANCH': 'x'}), method=method)
        self.assertEqual(rc, 0)
        args, _ = session.post.call_args
        self.assertEqual(args[0], f'{BASE}/job/legacy-job/buildWithParameters')

    def test_failure_result_maps_to_nonzero_rc(self):
        rc, output = self._execute(self._session(result='FAILURE'))
        self.assertEqual(rc, 1)
        self.assertIn('构建结果：FAILURE', output)

    def test_unstable_allowed_by_config(self):
        method = Method(name='m', code='m', provider='jenkins',
                        config={'allow_unstable': True})
        rc, _ = self._execute(self._session(result='UNSTABLE'), method=method)
        self.assertEqual(rc, 0)
        rc, _ = self._execute(self._session(result='UNSTABLE'))
        self.assertEqual(rc, 1)

    def test_trigger_http_error(self):
        rc, output = self._execute(self._session(trigger_status=404))
        self.assertEqual(rc, 1)
        self.assertIn('触发 Jenkins 构建失败：HTTP 404', output)

    def test_missing_connection_settings(self):
        SystemConfig.objects.filter(key='jenkins.api_token').delete()
        rc, output = self._execute(self._session())
        self.assertEqual(rc, 1)
        self.assertIn('系统配置', output)

    def test_validate_config(self):
        # 方法不绑定 job：空配置合法
        self.executor.validate_config({})
        self.executor.validate_config({'default_params': {'A': '1'}})
        with self.assertRaises(ValueError):
            self.executor.validate_config({'default_params': ['not-dict']})

    def test_method_serializer_accepts_jenkins_provider(self):
        from .serializers import MethodSerializer

        s = MethodSerializer(data={
            'name': 'CI 构建', 'code': 'ci-build', 'provider': 'jenkins', 'config': {},
        })
        self.assertTrue(s.is_valid(), s.errors)
        s = MethodSerializer(data={
            'name': 'CI 构建', 'code': 'ci-build2', 'provider': 'jenkins',
            'config': {'default_params': 'not-a-dict'},
        })
        self.assertFalse(s.is_valid())
        self.assertIn('config', s.errors)


class JenkinsViaAgentTests(TestCase):
    """Jenkins 经 Agent 通道：通道解析优先级、payload 形状、通道层失败。"""

    def setUp(self):
        from apps.agent_mgr.models import AgentNode, AgentRoute
        from apps.asset_mgr.models import Environment, ProjectGroup

        SystemConfig.objects.create(key='jenkins.url', value=BASE)
        SystemConfig.objects.create(key='jenkins.user', value='ops')
        SystemConfig.objects.create(key='jenkins.api_token', value='token-xyz')
        self.agent = AgentNode.objects.create(
            name='nat-gw-01', token_hash='x', status='online', channel_name='ch',
            meta={'capabilities': ['command', 'jenkins', 'playbook']})
        self.agent2 = AgentNode.objects.create(name='nat-gw-02', token_hash='y')
        self.pg = ProjectGroup.objects.create(name='CI 项目', code='ci-p1')
        self.env = Environment.objects.create(name='CI 正式', code='ci-prod')
        self.route_cls = AgentRoute
        self.method = Method(
            name='构建', code='build-app', provider='jenkins',
            config={'default_params': {'BRANCH': 'master'}})
        self.executor = get_executor('jenkins')

    def _execute(self, step, method=None, rc=0, output='ok'):
        """mock run_via_agent，返回 (执行结果, 调用参数 kwargs, payload)。"""
        with mock.patch('apps.agent_mgr.dispatch.run_via_agent',
                        return_value=(rc, output)) as dispatched:
            result = self.executor.execute(step, method or self.method)
        args, kwargs = dispatched.call_args
        return result, kwargs, args[1]

    def test_system_config_routes_to_agent(self):
        SystemConfig.objects.create(key='jenkins.agent', value='nat-gw-01')
        (code, output), kwargs, payload = self._execute(_step(params={
            'job_name': 'build-app', 'BRANCH': 'release'}))
        self.assertEqual(code, 0)
        self.assertIn('经 Agent nat-gw-01 · 系统配置', output)
        self.assertIn('ok', output)
        self.assertEqual(payload, {
            'step_type': 'jenkins', 'job_name': 'build-app',
            'params': {'BRANCH': 'release'}, 'allow_unstable': False, 'timeout': 60})
        self.assertEqual(kwargs['capability'], 'jenkins')
        self.assertEqual(kwargs['timeout'], 60)
        self.assertEqual(kwargs['step_order'], 1)

    def test_step_param_beats_method_and_config(self):
        SystemConfig.objects.create(key='jenkins.agent', value='nat-gw-02')
        self.method.config = dict(self.method.config, agent='nat-gw-02')
        (code, output), _, _ = self._execute(_step(params={
            'job_name': 'build-app', 'agent': 'nat-gw-01'}))
        self.assertEqual(code, 0)
        self.assertIn('经 Agent nat-gw-01 · 模块参数', output)

    def test_method_config_beats_system_config(self):
        SystemConfig.objects.create(key='jenkins.agent', value='nat-gw-02')
        self.method.config = dict(self.method.config, agent='nat-gw-01')
        (_, output), _, _ = self._execute(_step())
        self.assertIn('经 Agent nat-gw-01 · 方法配置', output)

    def test_agent_reserved_key_not_passed_to_jenkins(self):
        SystemConfig.objects.create(key='jenkins.agent', value='nat-gw-01')
        _, _, payload = self._execute(_step(params={
            'job_name': 'build-app', 'agent': 'nat-gw-01', 'artifact_version': '1.2.3',
            'BRANCH': 'dev'}))
        self.assertEqual(payload['params'], {'BRANCH': 'dev'})

    def test_route_rule_fallback(self):
        self.route_cls.objects.create(agent=self.agent, project_group=self.pg)
        (_, output), _, _ = self._execute(_step(project_group_id=self.pg.pk,
                                                environment_id=self.env.pk))
        self.assertIn('经 Agent nat-gw-01 · 路由规则', output)

    def test_custom_scope_dims_used_when_enabled(self):
        """模块自定义范围时按模块维度解析，不看任务全局维度。"""
        self.route_cls.objects.create(agent=self.agent, project_group=self.pg)
        step = _step(project_group_id=self.pg.pk, use_custom_scope=True)
        step.task = SimpleNamespace(project_group_id=None, environment_id=None)
        (_, output), _, _ = self._execute(step)
        self.assertIn('经 Agent nat-gw-01', output)

    def test_unknown_agent_name_fails_without_dispatch(self):
        SystemConfig.objects.create(key='jenkins.agent', value='ghost-gw')
        with mock.patch('apps.agent_mgr.dispatch.run_via_agent') as dispatched, \
                mock.patch('apps.method_mgr.executors.jenkins.requests.Session') as session:
            rc, output = self.executor.execute(_step(), self.method)
        self.assertEqual(rc, 1)
        self.assertIn('ghost-gw', output)
        dispatched.assert_not_called()
        session.assert_not_called()

    def test_allow_unstable_passed_through(self):
        SystemConfig.objects.create(key='jenkins.agent', value='nat-gw-01')
        self.method.config = dict(self.method.config, allow_unstable=True)
        _, _, payload = self._execute(_step())
        self.assertTrue(payload['allow_unstable'])

    def test_channel_failure_propagates_rc(self):
        SystemConfig.objects.create(key='jenkins.agent', value='nat-gw-01')
        (rc, output), _, _ = self._execute(
            _step(), rc=1, output='Agent「nat-gw-01」离线（最近心跳：从未），无法下发。')
        self.assertEqual(rc, 1)
        self.assertIn('离线', output)

    def test_offline_agent_real_dispatch_fails_fast(self):
        """不 mock dispatch：离线 agent 应在下发前失败，不落 AgentJob。"""
        from apps.agent_mgr.models import AgentJob

        SystemConfig.objects.create(key='jenkins.agent', value='nat-gw-02')  # 从未连接
        rc, output = self.executor.execute(_step(), self.method)
        self.assertEqual(rc, 1)
        self.assertIn('离线', output)
        self.assertFalse(AgentJob.objects.exists())

    def test_direct_when_no_agent_configured(self):
        """无任何 agent 配置与规则：仍走直连 REST（现状不变）。"""
        with mock.patch('apps.method_mgr.executors.jenkins.requests.Session') as session:
            session.return_value.post.return_value = FakeResp(status=500, text='boom')
            rc, output = self.executor.execute(_step(), self.method)
        self.assertEqual(rc, 1)
        self.assertIn('HTTP 500', output)
        self.assertFalse(session.return_value.trust_env)


class JumpServerExecutorTests(TestCase):
    """jumpserver 执行器：注册发现、目标为空、逐台结果与返回码。"""

    def setUp(self):
        self.method = Method.objects.create(name='注册跳板机', code='js-reg', provider='jumpserver')
        self.step = SimpleNamespace(params={}, timeout=60)

    def test_provider_registered(self):
        from .executors import available_providers
        self.assertIn('jumpserver', available_providers())

    def test_no_servers(self):
        from .executors import get_executor
        rc, out = get_executor('jumpserver').execute(self.step, self.method, servers=[])
        self.assertEqual(rc, 1)
        self.assertIn('没有可注册的目标主机', out)

    def test_missing_config(self):
        from apps.asset_mgr.models import Server
        from .executors import get_executor
        srv = Server.objects.create(name='js-x', private_ip='10.6.0.1')
        rc, out = get_executor('jumpserver').execute(self.step, self.method, servers=[srv])
        self.assertEqual(rc, 1)
        self.assertIn('jumpserver.url', out)

    def test_partial_failure_rc(self):
        from unittest import mock
        from apps.asset_mgr.models import Server
        from .executors import get_executor
        s1 = Server.objects.create(name='js-1', private_ip='10.6.0.2')
        s2 = Server.objects.create(name='js-2', private_ip='10.6.0.3')
        results = [
            {'id': s1.id, 'name': s1.name, 'ip': s1.private_ip, 'ok': True, 'msg': '注册成功'},
            {'id': s2.id, 'name': s2.name, 'ip': s2.private_ip, 'ok': False, 'msg': '注册失败：HTTP 400'},
        ]
        with mock.patch('apps.method_mgr.executors.jumpserver.register_servers', return_value=results):
            rc, out = get_executor('jumpserver').execute(self.step, self.method, servers=[s1, s2])
        self.assertEqual(rc, 1)
        self.assertIn('成功 1，失败 1', out)
