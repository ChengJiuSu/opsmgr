"""平台方法 Jenkins 执行器测试（requests 全程 mock，无需真实 Jenkins）。"""
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


def _step(params=None, timeout=60):
    """执行器只读取 TaskStep 的 params/timeout，用简单对象即可。

    job_name 由模块参数指定，默认给出以覆盖主流程用例。
    """
    return SimpleNamespace(params={'job_name': 'build-app'} if params is None else params,
                           timeout=timeout)


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
