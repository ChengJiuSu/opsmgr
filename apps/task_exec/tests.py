"""任务执行 + 平台方法模块测试。

命令/方法执行依赖系统已安装 ansible，通过 local 连接在 127.0.0.1 上运行；
方法执行使用临时 Ansible 项目目录（含带 tags 的剧本）。无 ansible 时相关用例跳过。
"""
import os
import shutil
import tempfile
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.asset_mgr.models import Server
from apps.method_mgr.models import Method

from .models import Credential, ExecutionTask, TaskExecutionLog
from .runner import _inventory_content
from .tasks import OUTPUT_PREVIEW_LIMIT

User = get_user_model()
ANSIBLE = bool(shutil.which('ansible') and shutil.which('ansible-playbook'))


def _make_ansible_project():
    proj = tempfile.mkdtemp(prefix='ansible_proj_')
    with open(os.path.join(proj, 'inventory.ini'), 'w') as f:
        f.write('localhost ansible_connection=local\n')
    with open(os.path.join(proj, 'site.yml'), 'w') as f:
        f.write(
            '- hosts: all\n'
            '  gather_facts: false\n'
            '  tasks:\n'
            '    - debug: {msg: "nginx restarted"}\n'
            '      tags: [nginx]\n'
            '    - debug: {msg: "redis restarted"}\n'
            '      tags: [redis]\n'
        )
    return proj


class InventoryTests(TestCase):
    def test_local_host_uses_local_connection(self):
        srv = Server.objects.create(name='local', ip='127.0.0.1')
        self.assertIn('ansible_connection=local', _inventory_content([srv], None, '/tmp'))

    def test_credential_password_injected(self):
        srv = Server.objects.create(name='web', ip='10.0.0.6')
        cred = Credential.objects.create(name='c1', username='ops', auth_type='password', password='secret', become=True)
        content = _inventory_content([srv], cred, '/tmp')
        self.assertIn('ansible_user=ops', content)
        self.assertIn('ansible_ssh_pass=secret', content)
        self.assertIn('ansible_become=true', content)

    def test_hostname_alias_and_service_groups(self):
        """inventory 以主机名为别名，并注入服务概要的 service_types 变量与分组。"""
        from apps.asset_mgr.models import ServerService, ServiceType

        game = ServiceType.objects.create(name='游戏服', code='game-server')
        redis = ServiceType.objects.create(name='Redis', code='redis')
        offline_svc = ServiceType.objects.create(name='网关', code='gateway')
        srv = Server.objects.create(name='game-01', ip='10.0.0.8')
        ServerService.objects.create(server=srv, service_type=game, status='planned')
        ServerService.objects.create(server=srv, service_type=redis, status='deployed')
        # offline 状态不进 inventory
        link = ServerService.objects.create(server=srv, service_type=offline_svc, status='planned')
        ServerService.objects.filter(pk=link.pk).update(status='offline')

        content = _inventory_content([srv], None, '/tmp')
        self.assertTrue(content.startswith('game-01 ansible_host=10.0.0.8'))
        self.assertIn('service_types="game-server,redis"', content)
        self.assertIn('[game-server]\ngame-01', content)
        self.assertIn('[redis]\ngame-01', content)
        self.assertNotIn('gateway', content)

    def test_alias_sanitized_and_deduplicated(self):
        """主机名含非法字符时净化为下划线；重名主机追加 IP 保证别名唯一。"""
        s1 = Server.objects.create(name='web 01', ip='10.0.0.11')
        s2 = Server.objects.create(name='web 01', ip='10.0.0.12')
        content = _inventory_content([s1, s2], None, '/tmp')
        self.assertIn('web_01 ansible_host=10.0.0.11', content)
        self.assertIn('web_01_10.0.0.12 ansible_host=10.0.0.12', content)


class MethodModelTests(TestCase):
    def _admin_client(self):
        u = User.objects.create(username='ma', is_staff=True, is_superuser=True)
        u.set_password('Pass!2345xyz')
        u.save()
        c = APIClient()
        r = c.post('/api/auth/login/', {'username': 'ma', 'password': 'Pass!2345xyz'}, format='json')
        c.credentials(HTTP_AUTHORIZATION='Bearer ' + r.json()['access'])
        return c

    def test_ansible_method_requires_playbook(self):
        c = self._admin_client()
        r = c.post('/api/exec/methods/', {
            'name': 'bad', 'code': 'bad', 'provider': 'ansible_playbook', 'config': {},
        }, format='json')
        self.assertEqual(r.status_code, 400)

    def test_unknown_provider_rejected(self):
        c = self._admin_client()
        r = c.post('/api/exec/methods/', {
            'name': 'x', 'code': 'x', 'provider': 'nope', 'config': {},
        }, format='json')
        self.assertEqual(r.status_code, 400)

    def test_non_staff_cannot_create_method(self):
        u = User.objects.create(username='u1')
        u.set_password('Pass!2345xyz')
        u.save()
        c = APIClient()
        r = c.post('/api/auth/login/', {'username': 'u1', 'password': 'Pass!2345xyz'}, format='json')
        c.credentials(HTTP_AUTHORIZATION='Bearer ' + r.json()['access'])
        r = c.post('/api/exec/methods/', {
            'name': 'x', 'code': 'x', 'provider': 'ansible_playbook', 'config': {'playbook': 'site.yml'},
        }, format='json')
        self.assertEqual(r.status_code, 403)


@override_settings(TASK_EXEC_BACKEND='sync')
class ExecutionAPITests(TestCase):
    def setUp(self):
        from apps.asset_mgr.models import ServerService, ServiceType

        self.admin = User.objects.create(username='admin1', is_staff=True, is_superuser=True)
        self.admin.set_password('Pass!2345xyz')
        self.admin.save()
        self.server = Server.objects.create(name='local', ip='127.0.0.1')
        # 执行权限前提：主机需在服务概要中已规划服务
        self.base_service = ServiceType.objects.create(name='基础服务', code='base')
        ServerService.objects.create(server=self.server, service_type=self.base_service, status='planned')

    def _client(self):
        c = APIClient()
        r = c.post('/api/auth/login/', {'username': 'admin1', 'password': 'Pass!2345xyz'}, format='json')
        c.credentials(HTTP_AUTHORIZATION='Bearer ' + r.json()['access'])
        return c

    def test_command_validation(self):
        r = self._client().post('/api/exec/tasks/', {
            'name': 'bad', 'task_type': 'command', 'server_ids': [self.server.id],
        }, format='json')
        self.assertEqual(r.status_code, 400)

    def test_command_task_rejects_unplanned_server(self):
        """执行权限校验：未规划任何服务类型的主机不允许作为执行目标。"""
        unplanned = Server.objects.create(name='raw-01', ip='10.0.0.99')
        r = self._client().post('/api/exec/tasks/', {
            'name': 'cmd', 'task_type': 'command', 'command': 'uptime',
            'server_ids': [self.server.id, unplanned.id],
        }, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('未规划任何服务类型', str(r.json()))
        self.assertIn('raw-01', str(r.json()))

    def test_run_command_guard_blocks_unplanned_server(self):
        """兜底校验：创建后规划被撤销（如服务下线）时，重跑在 runner 层拦截。"""
        from apps.asset_mgr.models import ServerService

        from . import runner

        task = ExecutionTask.objects.create(name='guard', task_type='command', command='uptime')
        task.servers.add(self.server)
        ServerService.objects.filter(server=self.server).update(status='offline')
        code, output = runner.run(task)
        self.assertEqual(code, 1)
        self.assertIn('未规划任何服务类型', output)

    def test_method_task_requires_method(self):
        r = self._client().post('/api/exec/tasks/', {
            'name': 'bad', 'task_type': 'method',
        }, format='json')
        self.assertEqual(r.status_code, 400)

    def test_method_task_requires_service_planned(self):
        """部署前置校验：方法关联服务类型时，目标主机必须已规划/已部署该服务。"""
        from apps.asset_mgr.models import ServerService, ServiceType

        game = ServiceType.objects.create(name='游戏服', code='game-server')
        method = Method.objects.create(
            name='部署游戏服', code='deploy-game', provider='ansible_playbook',
            config={'playbook': 'site.yml'}, service_type=game,
        )
        payload = {
            'name': 'deploy', 'task_type': 'method', 'method': method.id,
            'server_ids': [self.server.id],
        }
        # 未规划 → 拦截并提示先去配置管理规划
        r = self._client().post('/api/exec/tasks/', payload, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('未规划服务', str(r.json()))
        # 规划后放行（mock dispatch，避免真实执行 ansible）
        ServerService.objects.create(server=self.server, service_type=game, status='planned')
        with mock.patch('apps.task_exec.views.dispatch'):
            r = self._client().post('/api/exec/tasks/', payload, format='json')
        self.assertEqual(r.status_code, 201)

    def test_command_execution_runs(self):
        if not ANSIBLE:
            self.skipTest('ansible 未安装')
        r = self._client().post('/api/exec/tasks/', {
            'name': 'echo', 'task_type': 'command', 'command': 'echo ops_marker_123',
            'server_ids': [self.server.id],
        }, format='json')
        self.assertEqual(r.status_code, 201)
        task = ExecutionTask.objects.get(pk=r.json()['id'])
        self.assertEqual(task.status, 'success')
        self.assertIn('ops_marker_123', task.output)

    def test_method_execution_with_tags(self):
        if not ANSIBLE:
            self.skipTest('ansible 未安装')
        proj = _make_ansible_project()
        try:
            with override_settings(ANSIBLE_PROJECT_DIR=proj):
                method = Method.objects.create(
                    name='nginx', code='nginx', provider='ansible_playbook',
                    config={'playbook': 'site.yml', 'tags': 'nginx', 'inventory': 'inventory.ini'},
                )
                c = self._client()
                r = c.post('/api/exec/tasks/', {
                    'name': 'run nginx', 'task_type': 'method', 'method': method.id,
                }, format='json')
                self.assertEqual(r.status_code, 201)
                task = ExecutionTask.objects.get(pk=r.json()['id'])
                self.assertEqual(task.status, 'success')
                self.assertIn('nginx restarted', task.output)
                self.assertNotIn('redis restarted', task.output)

                # 任务级 tags 覆盖
                r = c.post('/api/exec/tasks/', {
                    'name': 'run redis', 'task_type': 'method', 'method': method.id, 'tags': 'redis',
                }, format='json')
                task2 = ExecutionTask.objects.get(pk=r.json()['id'])
                self.assertIn('redis restarted', task2.output)
                self.assertNotIn('nginx restarted', task2.output)
        finally:
            shutil.rmtree(proj, ignore_errors=True)

    def test_method_path_traversal_blocked(self):
        proj = _make_ansible_project()
        try:
            with override_settings(ANSIBLE_PROJECT_DIR=proj):
                method = Method.objects.create(
                    name='evil', code='evil', provider='ansible_playbook',
                    config={'playbook': '../../etc/passwd'},
                )
                r = self._client().post('/api/exec/tasks/', {
                    'name': 'evil', 'task_type': 'method', 'method': method.id,
                }, format='json')
                task = ExecutionTask.objects.get(pk=r.json()['id'])
                self.assertEqual(task.status, 'failed')
                self.assertIn('越出', task.output)
        finally:
            shutil.rmtree(proj, ignore_errors=True)

    def test_cloud_host_provider_reserved(self):
        method = Method.objects.create(
            name='reboot', code='reboot-vm', provider='cloud_host', config={'action': 'reboot'},
        )
        r = self._client().post('/api/exec/tasks/', {
            'name': 'cloud', 'task_type': 'method', 'method': method.id,
        }, format='json')
        task = ExecutionTask.objects.get(pk=r.json()['id'])
        self.assertEqual(task.status, 'failed')
        self.assertIn('预留', task.output)

    def test_execution_creates_log_record(self):
        """任务执行后必须落一条执行日志，结果与任务快照一致。"""
        method = Method.objects.create(
            name='reboot2', code='reboot-vm2', provider='cloud_host', config={'action': 'reboot'},
        )
        r = self._client().post('/api/exec/tasks/', {
            'name': 'log-test', 'task_type': 'method', 'method': method.id,
        }, format='json')
        task = ExecutionTask.objects.get(pk=r.json()['id'])
        logs = list(task.logs.all())
        self.assertEqual(len(logs), 1)
        log = logs[0]
        self.assertEqual(log.trigger_type, 'create')
        self.assertEqual(log.triggered_by, self.admin)
        self.assertEqual(log.status, task.status)
        self.assertEqual(log.return_code, task.return_code)
        self.assertEqual(log.output, task.output)
        self.assertIsNotNone(log.started_at)
        self.assertIsNotNone(log.finished_at)

    def test_rerun_appends_log_and_keeps_history(self):
        """重跑追加新日志，历史记录及其输出不被覆盖。"""
        method = Method.objects.create(
            name='reboot3', code='reboot-vm3', provider='cloud_host', config={'action': 'reboot'},
        )
        c = self._client()
        r = c.post('/api/exec/tasks/', {
            'name': 'rerun-test', 'task_type': 'method', 'method': method.id,
        }, format='json')
        task_id = r.json()['id']
        first_log = TaskExecutionLog.objects.get(task_id=task_id)
        first_output = first_log.output

        c.post(f'/api/exec/tasks/{task_id}/rerun/')
        logs = TaskExecutionLog.objects.filter(task_id=task_id).order_by('id')
        self.assertEqual(logs.count(), 2)
        self.assertEqual(logs[0].output, first_output)  # 历史保留
        self.assertEqual(logs[1].trigger_type, 'rerun')
        self.assertEqual(logs[1].triggered_by, self.admin)

    def test_long_output_persisted_to_file(self):
        """输出超过预览上限时全量落盘，数据库仅存截断预览。"""
        long_output = 'x' * (OUTPUT_PREVIEW_LIMIT + 500)
        log_dir = tempfile.mkdtemp(prefix='task_logs_')
        try:
            # 日志详情 API 读取时同样依赖 TASK_LOG_DIR，故 API 断言也须在 override 内执行
            with override_settings(TASK_LOG_DIR=log_dir):
                with mock.patch('apps.task_exec.runner.run', return_value=(0, long_output)):
                    task = ExecutionTask.objects.create(
                        name='long', task_type='command', command='echo', created_by=self.admin,
                    )
                    task.servers.add(self.server)
                    from .tasks import dispatch
                    log = dispatch(task, triggered_by=self.admin)
                    log.refresh_from_db()

                self.assertEqual(len(log.output), OUTPUT_PREVIEW_LIMIT)
                self.assertTrue(log.log_file)
                full_path = os.path.join(log_dir, log.log_file)
                with open(full_path, encoding='utf-8') as f:
                    self.assertEqual(f.read(), long_output)

                # 日志详情 API 返回完整输出
                r = self._client().get(f'/api/exec/tasks/{task.id}/logs/{log.id}/')
                self.assertEqual(len(r.json()['data']['output']), len(long_output))
        finally:
            shutil.rmtree(log_dir, ignore_errors=True)

    def test_logs_api_list(self):
        """日志列表接口返回执行历史，且不含输出大字段。"""
        method = Method.objects.create(
            name='reboot4', code='reboot-vm4', provider='cloud_host', config={'action': 'reboot'},
        )
        c = self._client()
        r = c.post('/api/exec/tasks/', {
            'name': 'list-test', 'task_type': 'method', 'method': method.id,
        }, format='json')
        task_id = r.json()['id']
        r = c.get(f'/api/exec/tasks/{task_id}/logs/')
        data = r.json()['data']
        self.assertEqual(len(data), 1)
        self.assertNotIn('output', data[0])
        self.assertEqual(data[0]['trigger_type'], 'create')
        self.assertEqual(data[0]['triggered_by_name'], 'admin1')

    def test_clean_task_logs_command(self):
        """清理命令删除过期日志记录及落盘文件，保留期内的不动。"""
        task = ExecutionTask.objects.create(name='old', task_type='command', command='echo')
        old_log = TaskExecutionLog.objects.create(task=task, status='success')
        new_log = TaskExecutionLog.objects.create(task=task, status='success')
        TaskExecutionLog.objects.filter(pk=old_log.pk).update(
            created_at=timezone.now() - timedelta(days=100),
        )

        log_dir = tempfile.mkdtemp(prefix='task_logs_')
        try:
            rel = os.path.join(str(task.id), f'{old_log.pk}.log')
            os.makedirs(os.path.join(log_dir, str(task.id)), exist_ok=True)
            with open(os.path.join(log_dir, rel), 'w') as f:
                f.write('old output')
            TaskExecutionLog.objects.filter(pk=old_log.pk).update(log_file=rel)

            with override_settings(TASK_LOG_DIR=log_dir, TASK_LOG_RETENTION_DAYS=90):
                call_command('clean_task_logs')

            self.assertFalse(TaskExecutionLog.objects.filter(pk=old_log.pk).exists())
            self.assertTrue(TaskExecutionLog.objects.filter(pk=new_log.pk).exists())
            self.assertFalse(os.path.exists(os.path.join(log_dir, rel)))
        finally:
            shutil.rmtree(log_dir, ignore_errors=True)

    def test_credential_secrets_not_exposed(self):
        c = self._client()
        c.post('/api/exec/credentials/', {
            'name': 'k1', 'username': 'root', 'auth_type': 'password', 'password': 'topsecret',
        }, format='json')
        listing = c.get('/api/exec/credentials/').json()['data']['results']
        self.assertNotIn('password', listing[0])
        self.assertTrue(listing[0]['has_password'])
