"""概览页测试：块级权限裁剪、各异常判定口径、单块失败降级。

重点不在「数字对不对」，而在两件容易回归的事：
  1. 无云控 / 无执行权限的账号不能从首页读到证书名、内网域名、任务名；
  2. 某一块查挂了整页仍要 200——概览是出事时第一个被打开的页面。
"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.agent_mgr.models import AgentNode
from apps.asset_mgr.models import CloudProvider
from apps.auth_mgr.models import Role
from apps.cloud_mgr.models import Certificate, CloudAccount, Domain
from apps.task_exec.models import ExecutionTask, TaskExecutionLog

User = get_user_model()

URL = '/api/dashboard/overview/'


def make_cloud_account():
    """云账号：证书与域名都要挂一个（provider 是字典外键，迁移已预置阿里云）。"""
    provider, _ = CloudProvider.objects.get_or_create(
        code='aliyun', defaults={'name': '阿里云'})
    return CloudAccount.objects.create(
        name='ali-main', provider=provider, access_key='AK', secret_key='SK')


class OverviewTestBase(TestCase):
    def _login(self, username, permissions=None, is_staff=False):
        """建号并登录。permissions=None 时不配角色（走 is_staff 回退）。"""
        user = User.objects.create(username=username, is_staff=is_staff)
        user.set_password('Pass!2345xyz')
        if permissions is not None:
            role = Role.objects.create(
                name=f'role-{username}', code=f'role-{username}', permissions=permissions)
            user.role = role
        user.save()
        client = APIClient()
        resp = client.post(
            '/api/auth/login/',
            {'username': username, 'password': 'Pass!2345xyz'}, format='json')
        client.credentials(HTTP_AUTHORIZATION='Bearer ' + resp.json()['access'])
        return client

    def _keys(self, client):
        body = client.get(URL).json()['data']
        return {
            'alerts': [b['key'] for b in body['alerts']],
            'todos': [b['key'] for b in body['todos']],
            'body': body,
        }


class OverviewPermissionTests(OverviewTestBase):
    """块级裁剪：每块只对有对应模块权限的人出现。"""

    def setUp(self):
        account = make_cloud_account()
        Certificate.objects.create(
            name='star.example.com', domains=['*.example.com'], cloud_account=account,
            status='issued', expires_at=timezone.now() + timedelta(days=5))
        Domain.objects.create(name='example.com', cloud_account=account, status='abnormal')
        task = ExecutionTask.objects.create(name='发布-prod', status='paused',
                                            pause_type='failed', started_at=timezone.now())
        TaskExecutionLog.objects.create(task=task, status='failed')
        AgentNode.objects.create(name='gw-hk', token_hash='x', enabled=True, status='offline')

    def test_full_permission_sees_all_blocks(self):
        client = self._login('all', {'cloud': 'write', 'exec': 'write', 'agent': 'write'})
        keys = self._keys(client)
        self.assertIn('cert_expiring', keys['alerts'])
        self.assertIn('domain_abnormal', keys['alerts'])
        self.assertIn('task_failed', keys['alerts'])
        self.assertIn('agent_offline', keys['alerts'])
        self.assertIn('task_paused', keys['todos'])
        self.assertTrue(keys['body']['agents']['visible'])

    def test_no_cloud_permission_hides_cert_and_domain(self):
        client = self._login('nocloud', {'exec': 'read', 'agent': 'read'})
        keys = self._keys(client)
        self.assertNotIn('cert_expiring', keys['alerts'])
        self.assertNotIn('domain_abnormal', keys['alerts'])
        self.assertNotIn('dns_approval', keys['todos'])
        # 证书名/域名不能以任何形式出现在响应里
        self.assertNotIn('star.example.com', str(keys['body']))
        self.assertNotIn('example.com', str(keys['body']))
        # 有 exec 权限的块照常给
        self.assertIn('task_failed', keys['alerts'])

    def test_no_exec_permission_hides_tasks(self):
        client = self._login('noexec', {'cloud': 'read'})
        keys = self._keys(client)
        self.assertNotIn('task_failed', keys['alerts'])
        self.assertNotIn('task_paused', keys['todos'])
        self.assertNotIn('发布-prod', str(keys['body']))
        self.assertFalse(keys['body']['running']['visible'])

    def test_no_agent_permission_hides_agent_panel(self):
        client = self._login('noagent', {'exec': 'read'})
        keys = self._keys(client)
        self.assertNotIn('agent_offline', keys['alerts'])
        self.assertFalse(keys['body']['agents']['visible'])
        self.assertEqual(keys['body']['agents']['nodes'], [])

    def test_anonymous_rejected(self):
        self.assertEqual(APIClient().get(URL).status_code, 401)


class CertAlertTests(OverviewTestBase):
    def setUp(self):
        self.account = make_cloud_account()
        self.client_ = self._login('ops', {'cloud': 'read'})

    def _cert(self, name, days, status='issued'):
        return Certificate.objects.create(
            name=name, domains=[name], cloud_account=self.account, status=status,
            expires_at=timezone.now() + timedelta(days=days))

    def test_31_days_not_alerted_29_days_alerted(self):
        self._cert('far.example.com', 31)
        self.assertEqual(self._keys(self.client_)['alerts'], [])
        self._cert('near.example.com', 29)
        block = self._keys(self.client_)['body']['alerts'][0]
        self.assertEqual(block['key'], 'cert_expiring')
        self.assertEqual(block['total'], 1)
        self.assertEqual(block['level'], 'warning')

    def test_already_expired_is_danger_with_negative_days(self):
        self._cert('old.example.com', -3)
        block = self._keys(self.client_)['body']['alerts'][0]
        self.assertEqual(block['level'], 'danger')
        self.assertLess(block['items'][0]['days_left'], 0)

    def test_issuing_stuck_reported(self):
        cert = self._cert('slow.example.com', 90, status='issuing')
        # updated_at 是 auto_now，直接改库绕过它
        Certificate.objects.filter(pk=cert.pk).update(
            updated_at=timezone.now() - Certificate.ISSUE_TIMEOUT - timedelta(minutes=1))
        self.assertIn('cert_issue_stuck', self._keys(self.client_)['alerts'])

    def test_issuing_within_timeout_not_reported(self):
        self._cert('fresh.example.com', 90, status='issuing')
        self.assertEqual(self._keys(self.client_)['alerts'], [])


class AgentAlertTests(OverviewTestBase):
    def setUp(self):
        self.client_ = self._login('ops', {'agent': 'read'})

    def test_stale_heartbeat_counts_as_offline_even_if_status_online(self):
        """kill -9 / 掉电时 disconnect 不跑，库里留假 online。心跳才是真相。"""
        AgentNode.objects.create(
            name='gw-dead', token_hash='x', enabled=True, status='online',
            last_heartbeat=timezone.now() - AgentNode.HEARTBEAT_TIMEOUT - timedelta(seconds=30))
        body = self._keys(self.client_)['body']
        self.assertIn('agent_offline', [b['key'] for b in body['alerts']])
        self.assertEqual(body['agents']['offline'], 1)
        self.assertEqual(body['agents']['nodes'][0]['status'], 'offline')

    def test_fresh_heartbeat_is_online(self):
        AgentNode.objects.create(
            name='gw-ok', token_hash='x', enabled=True, status='online',
            last_heartbeat=timezone.now())
        body = self._keys(self.client_)['body']
        self.assertEqual(body['alerts'], [])
        self.assertEqual(body['agents']['online'], 1)

    def test_disabled_node_offline_is_not_an_alert(self):
        AgentNode.objects.create(name='gw-off', token_hash='x', enabled=False, status='offline')
        self.assertEqual(self._keys(self.client_)['alerts'], [])


class TaskBlockTests(OverviewTestBase):
    def setUp(self):
        self.client_ = self._login('ops', {'exec': 'read'})

    def test_failed_window_is_24h(self):
        task = ExecutionTask.objects.create(name='t1')
        old = TaskExecutionLog.objects.create(task=task, status='failed')
        TaskExecutionLog.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(hours=25))
        self.assertEqual(self._keys(self.client_)['alerts'], [])
        TaskExecutionLog.objects.create(task=task, status='failed')
        self.assertIn('task_failed', self._keys(self.client_)['alerts'])

    def test_paused_todo_carries_log_id_and_waiting_time(self):
        task = ExecutionTask.objects.create(
            name='t2', status='paused', pause_type='confirm',
            started_at=timezone.now() - timedelta(minutes=10))
        log = TaskExecutionLog.objects.create(task=task, status='paused')
        item = self._keys(self.client_)['body']['todos'][0]['items'][0]
        self.assertEqual(item['log_id'], log.id)
        self.assertEqual(item['pause_type'], 'confirm')
        self.assertGreaterEqual(item['waiting_seconds'], 600)

    def test_running_task_reports_step_progress(self):
        task = ExecutionTask.objects.create(name='t3', status='running')
        task.steps.create(order=1, name='s1')
        task.steps.create(order=2, name='s2')
        TaskExecutionLog.objects.create(
            task=task, status='running', started_at=timezone.now(),
            steps_result=[{'order': 1, 'status': 'success'}])
        running = self._keys(self.client_)['body']['running']
        self.assertEqual(running['tasks'][0]['step_done'], 1)
        self.assertEqual(running['tasks'][0]['step_total'], 2)


class MfaTodoTests(OverviewTestBase):
    def test_mfa_off_todo_present_for_everyone(self):
        client = self._login('plain', {})
        self.assertIn('mfa_off', self._keys(client)['todos'])


class DegradeTests(OverviewTestBase):
    def test_one_broken_block_does_not_break_the_page(self):
        client = self._login('ops', {'cloud': 'read', 'exec': 'read'})
        with mock.patch('apps.dashboard.services.cert_alerts',
                        side_effect=RuntimeError('boom')):
            resp = client.get(URL)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()['data']
        self.assertNotIn('cert_expiring', [b['key'] for b in body['alerts']])

    def test_clean_environment_returns_empty_alerts(self):
        """全干净时 alerts 必须是空数组——前端据此显示「当前无异常」而不是空卡片。"""
        client = self._login('ops', {'cloud': 'read', 'exec': 'read', 'agent': 'read'})
        self.assertEqual(self._keys(client)['alerts'], [])
