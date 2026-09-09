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
from apps.artifact_mgr.models import Artifact
from apps.asset_mgr.models import (
    Channel, CloudProvider, Environment, ProjectGroup, Server, ServerService, ServiceType,
)
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


class AssetSummaryTests(OverviewTestBase):
    """资产盘子：主机分布 + 服务规划三态。"""

    def setUp(self):
        # 环境/渠道等字典由 0004 迁移预置，直接取现成的，避免撞 code 唯一约束
        self.prod, _ = Environment.objects.get_or_create(
            code='prod', defaults={'name': '生产'})
        self.test, _ = Environment.objects.get_or_create(
            code='test', defaults={'name': '测试'})
        self.group = ProjectGroup.objects.create(name='游戏A', code='ga')
        self.svc_type = ServiceType.objects.create(name='网关', code='gate')
        s1 = Server.objects.create(name='p1', private_ip='10.0.0.1',
                                   environment=self.prod, project_group=self.group)
        Server.objects.create(name='p2', private_ip='10.0.0.2', environment=self.prod)
        Server.objects.create(name='t1', private_ip='10.0.1.1', environment=self.test)
        # 没有维度的主机也要计进总数，分布里落到「未分配」
        Server.objects.create(name='x1', private_ip='10.0.2.1')
        ServerService.objects.create(server=s1, service_type=self.svc_type, status='planned')

    def test_distribution_and_service_states(self):
        client = self._login('ops', {'cmdb': 'read'})
        assets = self._keys(client)['body']['assets']
        self.assertTrue(assets['visible'])
        self.assertEqual(assets['total'], 4)
        by_env = {r['name']: r['count'] for r in assets['by_environment']}
        self.assertEqual(by_env['生产'], 2)
        self.assertEqual(by_env['测试'], 1)
        self.assertEqual(by_env['未分配'], 1)
        self.assertEqual(assets['services'], {'planned': 1, 'deployed': 0, 'offline': 0})

    def test_hidden_without_cmdb_permission(self):
        client = self._login('nocmdb', {'exec': 'read'})
        assets = self._keys(client)['body']['assets']
        self.assertFalse(assets['visible'])
        self.assertEqual(assets['total'], 0)
        self.assertNotIn('游戏A', str(assets))

    def test_distribution_tail_collapsed(self):
        """维度多到装不下时尾部并成「其他」，首页不铺二十条横条。"""
        for i in range(9):
            env = Environment.objects.create(name=f'env{i}', code=f'seed-env-{i}')
            Server.objects.create(name=f'h{i}', private_ip=f'10.1.0.{i + 1}', environment=env)
        client = self._login('ops', {'cmdb': 'read'})
        rows = self._keys(client)['body']['assets']['by_environment']
        self.assertEqual(len(rows), 7)          # DIST_LIMIT + 「其他」
        self.assertEqual(rows[-1]['name'], '其他')


class LatestArtifactTests(OverviewTestBase):
    """各维度 + 服务类型当前最新制品版本：每组只留 id 最大的一条，与 CD 注入同口径。"""

    def setUp(self):
        self.group = ProjectGroup.objects.create(name='游戏A', code='ga')
        self.channel, _ = Channel.objects.get_or_create(
            code='official', defaults={'name': '官方'})
        self.prod, _ = Environment.objects.get_or_create(
            code='prod', defaults={'name': '生产'})
        self.test, _ = Environment.objects.get_or_create(
            code='test', defaults={'name': '测试'})

    def _artifact(self, env, version, service_type=None):
        return Artifact.objects.create(
            project_group=self.group, channel=self.channel, environment=env,
            service_type=service_type,
            version=version, file_path=f'{version}.zip', original_name=f'{version}.zip',
            md5='0' * 32, size=1)

    def test_only_latest_per_dimension(self):
        self._artifact(self.prod, '1.0.0')
        self._artifact(self.prod, '1.0.1')
        self._artifact(self.test, '2.0.0')
        client = self._login('ops', {'artifact': 'read'})
        items = self._keys(client)['body']['artifacts']['items']
        self.assertEqual(len(items), 2)
        versions = {i['environment']: i['version'] for i in items}
        self.assertEqual(versions['生产'], '1.0.1')
        self.assertEqual(versions['测试'], '2.0.0')

    def test_latest_per_service_type(self):
        """同环境两个服务各显示一行：不按服务类型分组的话只看得见后上报的那个。"""
        from apps.asset_mgr.models import ServiceType

        gateway = ServiceType.objects.create(name='网关', code='db-gateway')
        logic = ServiceType.objects.create(name='逻辑服', code='db-logic')
        self._artifact(self.prod, '1.0.0', service_type=gateway)
        self._artifact(self.prod, '3.0.0', service_type=logic)   # 更晚上报
        client = self._login('ops2', {'artifact': 'read'})
        items = self._keys(client)['body']['artifacts']['items']
        self.assertEqual({(i['service_type'], i['version']) for i in items},
                         {('网关', '1.0.0'), ('逻辑服', '3.0.0')})

    def test_hidden_without_artifact_permission(self):
        self._artifact(self.prod, '1.0.0')
        client = self._login('noart', {'cmdb': 'read'})
        artifacts = self._keys(client)['body']['artifacts']
        self.assertFalse(artifacts['visible'])
        self.assertEqual(artifacts['items'], [])

    def test_reported_at_is_local_time(self):
        """上报时间按 TIME_ZONE 输出。

        概览是手拼 dict，DRF 的 JSONEncoder 不做时区转换，漏掉 local_dt 就会
        输出 UTC 的 ...Z——前端截字符串显示，页面上直接少 8 小时。
        """
        artifact = self._artifact(self.prod, '1.0.0')
        client = self._login('ops', {'artifact': 'read'})
        reported_at = self._keys(client)['body']['artifacts']['items'][0]['reported_at']
        self.assertNotIn('Z', reported_at)
        self.assertEqual(reported_at, timezone.localtime(artifact.reported_at).isoformat())


class ExecTrendTests(OverviewTestBase):
    """近 7 天执行趋势：按成败分色，未结束的单独一列。"""

    def setUp(self):
        self.task = ExecutionTask.objects.create(name='t')
        self.client_ = self._login('ops', {'exec': 'read'})

    def _log(self, status, days_ago=0):
        log = TaskExecutionLog.objects.create(task=self.task, status=status)
        if days_ago:
            TaskExecutionLog.objects.filter(pk=log.pk).update(
                created_at=timezone.now() - timedelta(days=days_ago))
        return log

    def test_always_seven_days_even_when_empty(self):
        trend = self._keys(self.client_)['body']['trend']
        self.assertTrue(trend['visible'])
        self.assertEqual(len(trend['days']), 7)
        self.assertEqual(sum(d['success'] + d['failed'] for d in trend['days']), 0)

    def test_counts_split_by_status(self):
        self._log('success')
        self._log('success')
        self._log('failed')
        self._log('running')
        today = self._keys(self.client_)['body']['trend']['days'][-1]
        self.assertEqual(today['success'], 2)
        self.assertEqual(today['failed'], 1)
        # 未结束的不算进成败，否则当天的数字过几分钟会自己变
        self.assertEqual(today['running'], 1)

    def test_older_than_window_excluded(self):
        self._log('success', days_ago=8)
        trend = self._keys(self.client_)['body']['trend']
        self.assertEqual(sum(d['success'] for d in trend['days']), 0)

    def test_hidden_without_exec_permission(self):
        client = self._login('noexec2', {'cmdb': 'read'})
        trend = self._keys(client)['body']['trend']
        self.assertFalse(trend['visible'])
        self.assertEqual(trend['days'], [])


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
