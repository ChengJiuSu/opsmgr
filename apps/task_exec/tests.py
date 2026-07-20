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

from .models import Credential, ExecutionTask, TaskExecutionLog, TaskStep
from . import runner
from .runner import _inventory_content
from .tasks import OUTPUT_PREVIEW_LIMIT

User = get_user_model()
ANSIBLE = bool(shutil.which('ansible') and shutil.which('ansible-playbook'))


def _make_ansible_project():
    proj = tempfile.mkdtemp(prefix='ansible_proj_')
    # roles 目录内的 yml 不是剧本，用于验证剧本列表接口的目录排除
    os.makedirs(os.path.join(proj, 'roles', 'demo', 'tasks'))
    with open(os.path.join(proj, 'roles', 'demo', 'tasks', 'main.yml'), 'w') as f:
        f.write('- debug: {msg: role task}\n')
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



@override_settings(TASK_EXEC_BACKEND='sync', TASK_FAILURE_WAIT_TIMEOUT=0)
class ExecutionAPITests(TestCase):
    """任务流 API 测试：任务=模块序列，新建/编辑仅保存，执行走 execute 动作。

    失败等待超时置 0（失败即中止），失败处理交互见 FailureHandlingTests。
    """

    def setUp(self):
        from apps.asset_mgr.models import Channel, Environment, ProjectGroup, ServerService, ServiceType

        self.admin = User.objects.create(username='admin1', is_staff=True, is_superuser=True)
        self.admin.set_password('Pass!2345xyz')
        self.admin.save()
        # 主机范围三维度必选：准备默认维度并挂到测试主机上
        self.pg = ProjectGroup.objects.create(name='主项目', code='ut-proj')
        self.ch = Channel.objects.create(name='官服渠道', code='ut-official')
        self.env = Environment.objects.create(name='单测环境', code='ut-env')
        self.server = Server.objects.create(
            name='local', ip='127.0.0.1',
            project_group=self.pg, channel=self.ch, environment=self.env,
        )
        # 执行权限前提：主机需在服务概要中已规划服务
        self.base_service = ServiceType.objects.create(name='基础服务', code='base')
        ServerService.objects.create(server=self.server, service_type=self.base_service, status='planned')

    def _client(self):
        c = APIClient()
        r = c.post('/api/auth/login/', {'username': 'admin1', 'password': 'Pass!2345xyz'}, format='json')
        c.credentials(HTTP_AUTHORIZATION='Bearer ' + r.json()['access'])
        return c

    @staticmethod
    def _step(**overrides):
        step = {'order': 1, 'name': '模块1', 'task_type': 'command', 'command': 'echo hi'}
        step.update(overrides)
        return step

    def _create_task(self, c, steps=None, **overrides):
        """保存一个任务流定义（默认带完整三维度），返回任务 id。"""
        payload = {
            'name': 'demo',
            'project_group': self.pg.id, 'channel': self.ch.id, 'environment': self.env.id,
            'steps': steps or [self._step()],
        }
        payload.update(overrides)
        r = c.post('/api/exec/tasks/', payload, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()['id']

    # ── 保存 / 编辑（嵌套模块）───────────────────────────────

    def test_create_saves_flow_without_executing(self):
        """新建仅保存任务流定义：不执行、不落日志，模块按序入库。"""
        c = self._client()
        task_id = self._create_task(c, steps=[
            self._step(name='步骤A'),
            self._step(order=2, name='步骤B', task_type='playbook', playbook='site.yml', tags='nginx', command=''),
        ])
        task = ExecutionTask.objects.get(pk=task_id)
        self.assertEqual(task.status, 'pending')
        self.assertEqual(task.logs.count(), 0)
        self.assertEqual(
            list(task.steps.values_list('order', 'name', 'task_type')),
            [(1, '步骤A', 'command'), (2, '步骤B', 'playbook')],
        )

    def test_steps_required_and_content_validated(self):
        """任务流至少一个模块；模块内容按类型校验。"""
        c = self._client()
        r = c.post('/api/exec/tasks/', {'name': 'empty', 'steps': []}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('至少需要一个任务模块', str(r.json()))

        cases = [
            (self._step(command=''), '必须填写 Shell 命令'),
            (self._step(task_type='playbook', command=''), '必须选择剧本方法'),
            # playbook tags 必选：不允许留空执行全部
            (self._step(task_type='playbook', playbook='site.yml', command=''), '必须选择一个 tags'),
            (self._step(task_type='method', command=''), '必须选择一个方法'),
        ]
        for step, keyword in cases:
            r = c.post('/api/exec/tasks/', {'name': 'bad', 'steps': [step]}, format='json')
            self.assertEqual(r.status_code, 400, step)
            self.assertIn(keyword, str(r.json()), step)

    def test_edit_replaces_steps_and_blocked_while_running(self):
        """编辑整体替换模块序列（序号归一）；执行中禁止编辑。"""
        c = self._client()
        task_id = self._create_task(c)
        r = c.patch(f'/api/exec/tasks/{task_id}/', {
            'name': 'renamed',
            'steps': [
                self._step(order=9, name='新B', command='echo b'),
                self._step(order=1, name='新C', command='echo c'),
            ],
        }, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        task = ExecutionTask.objects.get(pk=task_id)
        self.assertEqual(task.name, 'renamed')
        # 序号按提交顺序归一
        self.assertEqual(list(task.steps.values_list('order', 'name')), [(1, '新B'), (2, '新C')])

        ExecutionTask.objects.filter(pk=task_id).update(status='running')
        r = c.patch(f'/api/exec/tasks/{task_id}/', {'name': 'again'}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('无法编辑', str(r.json()))

    def test_legacy_task_migrated_to_single_step(self):
        """存量任务经 0006 迁移应为单模块任务流（迁移在测试库建库时执行，此处验证行为等价）。"""
        # 通过 ORM 直接构造"迁移后"形态，验证执行链路兼容单模块任务
        task = ExecutionTask.objects.create(name='legacy')
        TaskStep.objects.create(task=task, order=1, name='命令模块', task_type='command', command='echo 1')
        self.assertEqual(task.steps.count(), 1)

    # ── 主机匹配（全局 + 模块自定义）─────────────────────────

    def test_matched_servers_global_and_custom_steps(self):
        """matched_servers 返回全局匹配集与各模块主机来源；自定义模块附独立匹配结果。"""
        from apps.asset_mgr.models import ProjectGroup, ServerService

        c = self._client()
        pg_b = ProjectGroup.objects.create(name='项目B', code='proj-b')
        in_b = Server.objects.create(
            name='dim-01', ip='10.0.1.1', project_group=pg_b, channel=self.ch, environment=self.env,
        )
        ServerService.objects.create(server=in_b, service_type=self.base_service, status='deployed')
        # 未规划服务的主机即使维度匹配也不出现
        Server.objects.create(name='raw-01', ip='10.0.1.2', project_group=self.pg, channel=self.ch, environment=self.env)

        task_id = self._create_task(c, steps=[
            self._step(name='全局步骤'),
            self._step(order=2, name='定向步骤', use_custom_scope=True,
                       project_group=pg_b.id, channel=self.ch.id, environment=self.env.id),
        ])
        r = c.get(f'/api/exec/tasks/{task_id}/matched_servers/')
        data = r.json()['data']
        # 全局 = 任务维度（主项目）匹配：仅 local；raw-01 未规划不出现
        self.assertEqual([s['name'] for s in data['global']['results']], ['local'])
        self.assertEqual(data['steps'][0]['custom'], False)
        self.assertEqual(data['steps'][0]['total'], 1)
        self.assertEqual(data['steps'][1]['custom'], True)
        self.assertEqual([s['name'] for s in data['steps'][1]['servers']], ['dim-01'])

    # ── 执行入口校验 ──────────────────────────────────────

    def test_execute_selection_only_affects_global_steps(self):
        """勾选子集仅作用于全局范围模块；自定义范围模块按其维度全量执行。"""
        from apps.asset_mgr.models import ProjectGroup, ServerService

        c = self._client()
        pg_b = ProjectGroup.objects.create(name='项目B', code='proj-b')
        custom_srv = Server.objects.create(
            name='dim-01', ip='10.0.1.1', project_group=pg_b, channel=self.ch, environment=self.env,
        )
        ServerService.objects.create(server=custom_srv, service_type=self.base_service, status='planned')

        task_id = self._create_task(c, steps=[
            self._step(name='全局'),
            self._step(order=2, name='定向', use_custom_scope=True,
                       project_group=pg_b.id, channel=self.ch.id, environment=self.env.id),
        ])
        with mock.patch('apps.task_exec.runner.run', return_value=(0, 'ok', [])):
            r = c.post(f'/api/exec/tasks/{task_id}/execute/', {'server_ids': [self.server.id]}, format='json')
        self.assertEqual(r.status_code, 200)
        task = ExecutionTask.objects.get(pk=task_id)
        # 任务快照仅记录全局范围勾选（定向模块主机执行时实时解析）
        self.assertEqual([s.id for s in task.servers.all()], [self.server.id])

    def test_execute_rejects_out_of_scope_and_empty_step(self):
        c = self._client()
        raw = Server.objects.create(name='raw-02', ip='10.0.2.9')  # 未规划
        task_id = self._create_task(c)

        r = c.post(f'/api/exec/tasks/{task_id}/execute/', {'server_ids': [raw.id]}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('不在全局范围匹配集内', str(r.json()))

        # 全局勾空 + 存在全局范围模块 → 拒绝
        r = c.post(f'/api/exec/tasks/{task_id}/execute/', {'server_ids': []}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('未匹配/未勾选任何主机', str(r.json()))

    def test_execute_custom_step_no_match_rejected(self):
        """自定义范围模块维度无主机时拒绝执行。"""
        from apps.asset_mgr.models import Environment

        c = self._client()
        env = Environment.objects.create(name='预发', code='staging')
        task_id = self._create_task(c, steps=[
            self._step(name='定向空', use_custom_scope=True,
                       project_group=self.pg.id, channel=self.ch.id, environment=env.id),
        ])
        r = c.post(f'/api/exec/tasks/{task_id}/execute/', {}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('自定义范围维度未匹配到可执行主机', str(r.json()))

    def test_execute_blocked_while_running(self):
        c = self._client()
        task_id = self._create_task(c)
        ExecutionTask.objects.filter(pk=task_id).update(status='running')
        r = c.post(f'/api/exec/tasks/{task_id}/execute/', {}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('正在执行中', str(r.json()))

    def test_method_step_requires_service_planned(self):
        """部署前置校验（执行时逐模块）：方法模块关联服务类型时目标主机必须已规划该服务。"""
        from apps.asset_mgr.models import ServerService, ServiceType

        c = self._client()
        game = ServiceType.objects.create(name='游戏服', code='game-server')
        method = Method.objects.create(
            name='部署游戏服', code='deploy-game', provider='ansible_playbook',
            config={'playbook': 'site.yml'}, service_type=game,
        )
        task_id = self._create_task(c, steps=[
            self._step(name='部署', task_type='method', method=method.id, command=''),
        ])
        r = c.post(f'/api/exec/tasks/{task_id}/execute/', {}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('未规划服务', str(r.json()))
        self.assertIn('模块「部署」', str(r.json()))

        ServerService.objects.create(server=self.server, service_type=game, status='planned')
        with mock.patch('apps.task_exec.views.dispatch', return_value=mock.Mock(id=1)):
            r = c.post(f'/api/exec/tasks/{task_id}/execute/', {}, format='json')
        self.assertEqual(r.status_code, 200)

    # ── 任务流执行与日志 ──────────────────────────────────

    def test_flow_runs_steps_in_order(self):
        """混合类型任务流按序执行，输出分段，steps_result 逐模块落库。"""
        if not ANSIBLE:
            self.skipTest('ansible 未安装')
        proj = _make_ansible_project()
        try:
            with override_settings(ANSIBLE_PROJECT_DIR=proj):
                c = self._client()
                task_id = self._create_task(c, steps=[
                    self._step(name='报数', command='echo ops_step_one'),
                    self._step(order=2, name='重启redis', task_type='playbook',
                               playbook='site.yml', tags='redis', command=''),
                ])
                r = c.post(f'/api/exec/tasks/{task_id}/execute/', {}, format='json')
                self.assertEqual(r.status_code, 200)
                task = ExecutionTask.objects.get(pk=task_id)
                self.assertEqual(task.status, 'success')
                self.assertIn('模块 1/2：报数', task.output)
                self.assertIn('ops_step_one', task.output)
                self.assertIn('模块 2/2：重启redis', task.output)
                self.assertIn('redis restarted', task.output)
                # 顺序：模块1输出在模块2之前
                self.assertLess(task.output.index('ops_step_one'), task.output.index('redis restarted'))

                log = task.logs.get()
                self.assertEqual([s['status'] for s in log.steps_result], ['success', 'success'])
                self.assertEqual([s['name'] for s in log.steps_result], ['报数', '重启redis'])
        finally:
            shutil.rmtree(proj, ignore_errors=True)

    def test_flow_aborts_on_step_failure(self):
        """模块失败即中止：后续模块标记 skipped，任务失败。"""
        if not ANSIBLE:
            self.skipTest('ansible 未安装')
        c = self._client()
        task_id = self._create_task(c, steps=[
            self._step(name='会失败', command='false'),
            self._step(order=2, name='不该执行', command='echo should_not_run'),
        ])
        r = c.post(f'/api/exec/tasks/{task_id}/execute/', {}, format='json')
        self.assertEqual(r.status_code, 200)
        task = ExecutionTask.objects.get(pk=task_id)
        self.assertEqual(task.status, 'failed')
        self.assertIn('中止后续 1 个模块', task.output)
        self.assertNotIn('should_not_run', task.output)
        log = task.logs.get()
        self.assertEqual([s['status'] for s in log.steps_result], ['failed', 'skipped'])

    def test_serial_mode_adds_forks(self):
        """串行执行：命令模块的 ansible 命令行携带 --forks 1，并发不带。"""
        c = self._client()
        task_id = self._create_task(c, exec_mode='serial')
        with mock.patch('apps.task_exec.runner.run_subprocess', return_value=(0, 'ok')) as m:
            c.post(f'/api/exec/tasks/{task_id}/execute/', {}, format='json')
        cmd = m.call_args[0][0]
        self.assertIn('--forks', cmd)
        self.assertEqual(cmd[cmd.index('--forks') + 1], '1')

        # 并发（默认）：按任务并发数分批，默认 10
        task2 = self._create_task(c)
        with mock.patch('apps.task_exec.runner.run_subprocess', return_value=(0, 'ok')) as m:
            c.post(f'/api/exec/tasks/{task2}/execute/', {}, format='json')
        cmd = m.call_args[0][0]
        self.assertEqual(cmd[cmd.index('--forks') + 1], '10')

        # 自定义并发数
        task3 = self._create_task(c, forks=25)
        with mock.patch('apps.task_exec.runner.run_subprocess', return_value=(0, 'ok')) as m:
            c.post(f'/api/exec/tasks/{task3}/execute/', {}, format='json')
        cmd = m.call_args[0][0]
        self.assertEqual(cmd[cmd.index('--forks') + 1], '25')

    def test_method_executor_serial_forks_and_step_params(self):
        """方法模块执行器：按 step 读取 tags/timeout，exec_mode 透传任务级串行。"""
        from apps.method_mgr.executors import get_executor

        proj = _make_ansible_project()
        try:
            with override_settings(ANSIBLE_PROJECT_DIR=proj):
                method = Method.objects.create(
                    name='m-serial', code='m-serial', provider='ansible_playbook',
                    config={'playbook': 'site.yml', 'tags': 'nginx'},
                )
                task = ExecutionTask.objects.create(name='t', exec_mode='serial')
                step = TaskStep.objects.create(
                    task=task, order=1, name='m', task_type='method', method=method, tags='redis',
                )
                with mock.patch(
                    'apps.method_mgr.executors.ansible_playbook.run_subprocess', return_value=(0, 'ok'),
                ) as m:
                    get_executor('ansible_playbook').execute(step, method, inventory_path='/tmp/inv.ini')
                cmd = m.call_args[0][0]
                self.assertIn('--forks', cmd)
                # 模块级 tags 覆盖方法默认
                self.assertEqual(cmd[cmd.index('--tags') + 1], 'redis')
        finally:
            shutil.rmtree(proj, ignore_errors=True)

    def test_runner_guard_blocks_unplanned_server(self):
        """兜底校验：执行排队期间规划被撤销（服务下线）时，runner 层拦截。"""
        from apps.asset_mgr.models import ServerService

        from . import runner

        task = ExecutionTask.objects.create(name='guard')
        TaskStep.objects.create(task=task, order=1, name='cmd', task_type='command', command='uptime')
        task.servers.add(self.server)
        ServerService.objects.filter(server=self.server).update(status='offline')
        code, output, results = runner.run(task)
        self.assertEqual(code, 1)
        self.assertIn('未规划任何服务类型', output)
        self.assertEqual(results[0]['status'], 'failed')

    def test_playbook_path_traversal_blocked(self):
        proj = _make_ansible_project()
        try:
            with override_settings(ANSIBLE_PROJECT_DIR=proj):
                c = self._client()
                task_id = self._create_task(c, steps=[
                    self._step(name='evil', task_type='playbook', playbook='../../etc/passwd', tags='x', command=''),
                ])
                c.post(f'/api/exec/tasks/{task_id}/execute/', {}, format='json')
                task = ExecutionTask.objects.get(pk=task_id)
                self.assertEqual(task.status, 'failed')
                self.assertIn('越出', task.output)
        finally:
            shutil.rmtree(proj, ignore_errors=True)

    def test_cloud_host_provider_reserved(self):
        method = Method.objects.create(
            name='reboot', code='reboot-vm', provider='cloud_host', config={'action': 'reboot'},
        )
        c = self._client()
        task_id = self._create_task(c, steps=[
            self._step(name='cloud', task_type='method', method=method.id, command=''),
        ])
        c.post(f'/api/exec/tasks/{task_id}/execute/', {}, format='json')
        task = ExecutionTask.objects.get(pk=task_id)
        self.assertEqual(task.status, 'failed')
        self.assertIn('预留', task.output)

    def test_playbook_list_api(self):
        """剧本列表接口：仅返回项目目录内剧本，排除 roles 等目录；仅管理员可用。"""
        proj = _make_ansible_project()
        try:
            with override_settings(ANSIBLE_PROJECT_DIR=proj):
                r = self._client().get('/api/exec/playbooks/')
                self.assertEqual(r.status_code, 200)
                results = r.json()['data']['results']
                self.assertIn('site.yml', results)
                self.assertFalse(any(p.startswith('roles') for p in results))

                u = User.objects.create(username='viewer1')
                u.set_password('Pass!2345xyz')
                u.save()
                c = APIClient()
                login = c.post('/api/auth/login/', {'username': 'viewer1', 'password': 'Pass!2345xyz'}, format='json')
                c.credentials(HTTP_AUTHORIZATION='Bearer ' + login.json()['access'])
                self.assertEqual(c.get('/api/exec/playbooks/').status_code, 403)
        finally:
            shutil.rmtree(proj, ignore_errors=True)

    def test_execute_records_log_with_snapshot(self):
        """执行落日志：trigger=execute，含全局主机快照、执行方式与模块结果。"""
        c = self._client()
        task_id = self._create_task(c, exec_mode='serial')
        with mock.patch('apps.task_exec.runner.run', return_value=(0, 'ok', [{'order': 1, 'status': 'success'}])):
            r = c.post(f'/api/exec/tasks/{task_id}/execute/', {}, format='json')
        self.assertEqual(r.status_code, 200)
        task = ExecutionTask.objects.get(pk=task_id)
        log = task.logs.get()
        self.assertEqual(log.trigger_type, 'execute')
        self.assertEqual(log.exec_mode, 'serial')
        self.assertEqual(log.servers_snapshot, [{'id': self.server.id, 'name': 'local', 'ip': '127.0.0.1'}])
        self.assertEqual(log.steps_result, [{'order': 1, 'status': 'success'}])
        self.assertEqual(log.triggered_by, self.admin)

    def test_repeat_execute_appends_log(self):
        """反复执行追加日志，历史输出不被覆盖。"""
        c = self._client()
        task_id = self._create_task(c)
        with mock.patch('apps.task_exec.runner.run', side_effect=[(0, 'first run', []), (1, 'second run', [])]):
            c.post(f'/api/exec/tasks/{task_id}/execute/', {}, format='json')
            c.post(f'/api/exec/tasks/{task_id}/execute/', {}, format='json')
        logs = TaskExecutionLog.objects.filter(task_id=task_id).order_by('id')
        self.assertEqual(logs.count(), 2)
        self.assertEqual(logs[0].output, 'first run')
        self.assertEqual(logs[1].output, 'second run')
        self.assertEqual(ExecutionTask.objects.get(pk=task_id).status, 'failed')

    def test_long_output_persisted_to_file(self):
        """输出超过预览上限时全量落盘，数据库仅存截断预览。"""
        long_output = 'x' * (OUTPUT_PREVIEW_LIMIT + 500)
        log_dir = tempfile.mkdtemp(prefix='task_logs_')
        try:
            with override_settings(TASK_LOG_DIR=log_dir):
                with mock.patch('apps.task_exec.runner.run', return_value=(0, long_output, [])):
                    task = ExecutionTask.objects.create(name='long', created_by=self.admin)
                    TaskStep.objects.create(task=task, order=1, name='cmd', task_type='command', command='echo')
                    task.servers.add(self.server)
                    from .tasks import dispatch
                    log = dispatch(task, triggered_by=self.admin)
                    log.refresh_from_db()

                self.assertEqual(len(log.output), OUTPUT_PREVIEW_LIMIT)
                self.assertTrue(log.log_file)
                full_path = os.path.join(log_dir, log.log_file)
                with open(full_path, encoding='utf-8') as f:
                    self.assertEqual(f.read(), long_output)

                r = self._client().get(f'/api/exec/tasks/{task.id}/logs/{log.id}/')
                self.assertEqual(len(r.json()['data']['output']), len(long_output))
        finally:
            shutil.rmtree(log_dir, ignore_errors=True)

    def test_logs_api_list(self):
        """日志列表接口返回执行历史（含主机数/执行方式/模块结果），不含输出大字段。"""
        c = self._client()
        task_id = self._create_task(c, name='list-test')
        with mock.patch('apps.task_exec.runner.run', return_value=(0, 'ok', [{'order': 1, 'status': 'success'}])):
            c.post(f'/api/exec/tasks/{task_id}/execute/', {}, format='json')
        r = c.get(f'/api/exec/tasks/{task_id}/logs/')
        data = r.json()['data']
        self.assertEqual(len(data), 1)
        self.assertNotIn('output', data[0])
        self.assertEqual(data[0]['trigger_type'], 'execute')
        self.assertEqual(data[0]['server_count'], 1)
        self.assertEqual(data[0]['exec_mode'], 'parallel')  # 默认主机执行方式为并发（分批）
        self.assertEqual(data[0]['steps_result'][0]['status'], 'success')

    def test_task_type_filter_matches_step_type(self):
        """列表 task_type 筛选 = 包含该类型模块的任务。"""
        c = self._client()
        self._create_task(c, name='cmd-only')
        self._create_task(c, name='with-pb', steps=[
            self._step(name='a'),
            self._step(order=2, name='b', task_type='playbook', playbook='site.yml', tags='nginx', command=''),
        ])
        names = [t['name'] for t in c.get('/api/exec/tasks/', {'task_type': 'playbook'}).json()['data']['results']]
        self.assertEqual(names, ['with-pb'])
        names = [t['name'] for t in c.get('/api/exec/tasks/', {'task_type': 'command'}).json()['data']['results']]
        self.assertEqual(sorted(names), ['cmd-only', 'with-pb'])

    def test_clean_task_logs_command(self):
        """清理命令删除过期日志记录及落盘文件，保留期内的不动。"""
        task = ExecutionTask.objects.create(name='old')
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

    # ── 主机范围三维度必选 ─────────────────────────────────

    def test_task_requires_all_dimensions(self):
        """任务全局主机范围不再支持留空=不限，三维度必选。"""
        c = self._client()
        r = c.post('/api/exec/tasks/', {'name': 'no-dim', 'steps': [self._step()]}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('必须选择项目组', str(r.json()))

        r = c.post('/api/exec/tasks/', {
            'name': 'partial', 'project_group': self.pg.id, 'channel': self.ch.id,
            'steps': [self._step()],
        }, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('必须选择环境', str(r.json()))

    def test_custom_scope_requires_all_dimensions(self):
        """模块自定义主机范围同策略：三维度必选。"""
        c = self._client()
        r = c.post('/api/exec/tasks/', {
            'name': 'bad', 'project_group': self.pg.id, 'channel': self.ch.id, 'environment': self.env.id,
            'steps': [self._step(use_custom_scope=True, project_group=self.pg.id)],
        }, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('自定义主机范围必须选择', str(r.json()))

    def test_execute_legacy_task_without_dims_rejected(self):
        """兜底：存量维度不全的任务不予执行，提示补全。"""
        task = ExecutionTask.objects.create(name='legacy')
        TaskStep.objects.create(task=task, order=1, name='cmd', task_type='command', command='echo')
        r = self._client().post(f'/api/exec/tasks/{task.id}/execute/', {}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('主机范围', str(r.json()))

    # ── 变量管理与执行注入 ─────────────────────────────────

    def _variable_payload(self, **overrides):
        payload = {
            'key': 'app_env', 'value': 'prod', 'description': '环境标识',
            'project_group': self.pg.id, 'channel': self.ch.id, 'environment': self.env.id,
        }
        payload.update(overrides)
        return payload

    def test_variable_crud_and_key_validation(self):
        """变量 CRUD：key 须为合法 ansible 变量名、值须为合法 YAML、三维度必选；普通用户只读。"""
        c = self._client()
        r = c.post('/api/exec/variables/', self._variable_payload(), format='json')
        self.assertEqual(r.status_code, 201)
        r = c.post('/api/exec/variables/', self._variable_payload(key='1bad-key'), format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('变量名', str(r.json()))
        # 非法 YAML
        r = c.post('/api/exec/variables/', self._variable_payload(key='bad_yaml', value='a: [1,'), format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('YAML', str(r.json()))
        # 生效条件三维度必选
        r = c.post('/api/exec/variables/', {'key': 'no_dim', 'value': 'x'}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('必须选择项目组', str(r.json()))

        u = User.objects.create(username='viewer2')
        u.set_password('Pass!2345xyz')
        u.save()
        vc = APIClient()
        login = vc.post('/api/auth/login/', {'username': 'viewer2', 'password': 'Pass!2345xyz'}, format='json')
        vc.credentials(HTTP_AUTHORIZATION='Bearer ' + login.json()['access'])
        self.assertEqual(vc.get('/api/exec/variables/').status_code, 200)  # 只读（含 Jenkins 等外部拉取场景）
        self.assertEqual(vc.post('/api/exec/variables/', self._variable_payload(key='x'), format='json').status_code, 403)

    def test_variable_versions_and_rollback(self):
        """版本历史：每次保存落快照、默认保留 10 版、回滚套用快照并生成新版本。"""
        c = self._client()
        var_id = c.post(
            '/api/exec/variables/', self._variable_payload(key='cfg', value='v: 1'), format='json',
        ).json()['id']

        c.patch(f'/api/exec/variables/{var_id}/', {'value': 'v: 2'}, format='json')
        versions = c.get(f'/api/exec/variables/{var_id}/versions/').json()['data']
        self.assertEqual([v['version'] for v in versions], [2, 1])
        self.assertEqual(versions[1]['value'], 'v: 1')

        # 回滚到 v1：值恢复且追加新版本 v3
        r = c.post(f'/api/exec/variables/{var_id}/rollback/', {'version': 1}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['data']['value'], 'v: 1')
        versions = c.get(f'/api/exec/variables/{var_id}/versions/').json()['data']
        self.assertEqual(versions[0]['version'], 3)
        # 不存在的版本
        r = c.post(f'/api/exec/variables/{var_id}/rollback/', {'version': 99}, format='json')
        self.assertEqual(r.status_code, 400)

        # 保留上限：连续更新超过 10 版后只留最近 10 条
        for i in range(12):
            c.patch(f'/api/exec/variables/{var_id}/', {'value': f'v: {i + 10}'}, format='json')
        versions = c.get(f'/api/exec/variables/{var_id}/versions/').json()['data']
        self.assertEqual(len(versions), 10)
        self.assertEqual(versions[0]['version'], 15)  # 3 + 12

    def test_method_step_params_json_roundtrip(self):
        """方法模块参数：JSON 对象保存回显；非对象类型 400。"""
        method = Method.objects.create(
            name='reboot-p', code='reboot-p', provider='cloud_host', config={'action': 'reboot'},
        )
        c = self._client()
        task_id = self._create_task(c, steps=[
            self._step(name='带参', task_type='method', method=method.id, command='',
                       params={'region': 'cn-sh', 'count': 2}),
        ])
        r = c.get(f'/api/exec/tasks/{task_id}/')
        self.assertEqual(r.json()['steps'][0]['params'], {'region': 'cn-sh', 'count': 2})

        r = c.post('/api/exec/tasks/', {
            'name': 'bad-params',
            'project_group': self.pg.id, 'channel': self.ch.id, 'environment': self.env.id,
            'steps': [self._step(name='坏参', task_type='method', method=method.id, command='',
                                 params=['not', 'a', 'dict'])],
        }, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('JSON 对象', str(r.json()))

    def test_variables_injected_by_dimensions(self):
        """执行注入：仅加载与模块有效维度精确匹配的启用变量，YAML 解析后 JSON 注入。"""
        from apps.asset_mgr.models import Environment

        from .models import ExecVariable

        other_env = Environment.objects.create(name='预发2', code='staging2')
        ExecVariable.objects.create(
            key='app_conf', value='mode: prod\nports:\n  - 80\n  - 443',
            project_group=self.pg, channel=self.ch, environment=self.env,
        )
        ExecVariable.objects.create(  # 维度不匹配：不注入
            key='other_dim_var', value='x',
            project_group=self.pg, channel=self.ch, environment=other_env,
        )
        ExecVariable.objects.create(  # 停用：不注入
            key='disabled_var', value='x', enabled=False,
            project_group=self.pg, channel=self.ch, environment=self.env,
        )
        c = self._client()
        task_id = self._create_task(c, steps=[self._step(extra_vars='override=1')])
        with mock.patch('apps.task_exec.runner.run_subprocess', return_value=(0, 'ok')) as m:
            c.post(f'/api/exec/tasks/{task_id}/execute/', {}, format='json')
        cmd = m.call_args[0][0]
        idxs = [i for i, a in enumerate(cmd) if a == '--extra-vars']
        self.assertEqual(len(idxs), 2)
        injected = cmd[idxs[0] + 1]
        self.assertIn('"app_conf"', injected)
        self.assertIn('"mode": "prod"', injected)   # YAML 解析为结构化 JSON
        self.assertIn('[80, 443]', injected)
        self.assertNotIn('other_dim_var', injected)
        self.assertNotIn('disabled_var', injected)
        self.assertEqual(cmd[idxs[1] + 1], 'override=1')  # 模块变量在后，ansible 同名覆盖


@override_settings(TASK_EXEC_BACKEND='sync', TASK_FAILURE_WAIT_TIMEOUT=0)
class ArtifactInjectTests(TestCase):
    """CD 制品变量注入：按模块有效维度取最新制品，params.artifact_version 可锁定版本。"""

    # 复用任务流 API 测试的环境准备与辅助方法（不继承，避免父类用例重复执行）
    setUp = ExecutionAPITests.setUp
    _client = ExecutionAPITests._client
    _step = staticmethod(ExecutionAPITests._step)
    _create_task = ExecutionAPITests._create_task

    def _make_artifact(self, version, **overrides):
        from apps.artifact_mgr.models import Artifact

        fields = dict(
            project_group=self.pg, channel=self.ch, environment=self.env, version=version,
            file_path=f'p/c/e/{version}/app.tar.gz', original_name='app.tar.gz',
            md5='a' * 32, size=10,
        )
        fields.update(overrides)
        return Artifact.objects.create(**fields)

    def _run_step_cmd(self, c, task_id):
        """mock 子进程执行一次任务，返回捕获到的 ansible 命令行。"""
        with mock.patch('apps.task_exec.runner.run_subprocess', return_value=(0, 'ok')) as m:
            c.post(f'/api/exec/tasks/{task_id}/execute/', {}, format='json')
        return m.call_args[0][0]

    def test_latest_artifact_injected_as_extra_vars(self):
        self._make_artifact('1.0.0')
        latest = self._make_artifact('1.1.0', md5='b' * 32)
        c = self._client()
        cmd = self._run_step_cmd(c, self._create_task(c))
        idxs = [i for i, a in enumerate(cmd) if a == '--extra-vars']
        self.assertEqual(len(idxs), 1)
        injected = cmd[idxs[0] + 1]
        self.assertIn('"package_version": "1.1.0"', injected)
        self.assertIn(f'"package_md5": "{"b" * 32}"', injected)
        self.assertIn(f'/api/artifacts/{latest.id}/download/?sig=', injected)

    def test_no_artifact_no_injection(self):
        c = self._client()
        cmd = self._run_step_cmd(c, self._create_task(c))
        self.assertNotIn('--extra-vars', cmd)

    def test_locked_version_injected(self):
        target = self._make_artifact('1.0.0')
        self._make_artifact('1.1.0')
        c = self._client()
        task_id = self._create_task(c, steps=[self._step(params={'artifact_version': '1.0.0'})])
        cmd = self._run_step_cmd(c, task_id)
        injected = cmd[cmd.index('--extra-vars') + 1]
        self.assertIn('"package_version": "1.0.0"', injected)
        self.assertIn(f'/api/artifacts/{target.id}/download/', injected)

    def test_locked_version_missing_fails_step(self):
        self._make_artifact('1.0.0')
        c = self._client()
        task_id = self._create_task(c, steps=[self._step(params={'artifact_version': '9.9.9'})])
        with mock.patch('apps.task_exec.runner.run_subprocess', return_value=(0, 'ok')):
            c.post(f'/api/exec/tasks/{task_id}/execute/', {}, format='json')
        task = ExecutionTask.objects.get(pk=task_id)
        self.assertEqual(task.status, 'failed')
        self.assertIn('未找到锁定版本的构建制品', task.output)
        log = TaskExecutionLog.objects.get(task=task)
        self.assertEqual(log.steps_result[0]['status'], 'failed')

    def test_artifact_vars_follow_custom_scope_dims(self):
        """自定义范围模块按模块自身维度取制品，而非任务全局维度。"""
        from apps.asset_mgr.models import Environment, ServerService

        other_env = Environment.objects.create(name='预发', code='ut-staging')
        other_srv = Server.objects.create(
            name='stage', ip='127.0.0.1',
            project_group=self.pg, channel=self.ch, environment=other_env,
        )
        ServerService.objects.create(server=other_srv, service_type=self.base_service, status='planned')
        self._make_artifact('1.0.0')  # 全局维度制品
        scoped = self._make_artifact('2.0.0', environment=other_env)
        c = self._client()
        task_id = self._create_task(c, steps=[self._step(
            use_custom_scope=True,
            project_group=self.pg.id, channel=self.ch.id, environment=other_env.id,
        )])
        cmd = self._run_step_cmd(c, task_id)
        injected = cmd[cmd.index('--extra-vars') + 1]
        self.assertIn('"package_version": "2.0.0"', injected)
        self.assertIn(f'/api/artifacts/{scoped.id}/download/', injected)


class PauseStepTests(TestCase):
    """暂停确认模块：runner 等待确认（继续/中止/超时）与 confirm 接口。"""

    def setUp(self):
        self.user = User.objects.create(username='admin_p', is_staff=True, is_superuser=True)
        self.user.set_password('Pass!2345xyz')
        self.user.save()
        self.task = ExecutionTask.objects.create(name='暂停流', created_by=self.user)
        self.step = TaskStep.objects.create(
            task=self.task, order=1, name='上线前确认', task_type='pause', timeout=30,
        )

    def _client(self, username='admin_p'):
        c = APIClient()
        r = c.post('/api/auth/login/', {'username': username, 'password': 'Pass!2345xyz'}, format='json')
        c.credentials(HTTP_AUTHORIZATION='Bearer ' + r.json()['access'])
        return c

    def _run_with_action(self, action):
        """mock 等待间隔的 sleep：首轮未读到动作进入 sleep 时写入确认动作，
        下一轮轮询即消费（单线程注入，规避 SQLite 测试库跨线程锁）。"""
        task_pk = self.task.pk

        def _inject_action(_seconds):
            ExecutionTask.objects.filter(pk=task_pk).update(pause_action=action)

        with mock.patch.object(runner.time, 'sleep', side_effect=_inject_action):
            return runner.run(self.task)

    def test_pause_continue(self):
        rc, output, results = self._run_with_action('continue')
        self.assertEqual(rc, 0)
        self.assertIn('已人工确认', output)
        self.assertEqual(results[0]['status'], 'success')
        self.assertIsNone(results[0]['server_count'])
        self.task.refresh_from_db()
        # 确认后状态恢复 running（最终状态由 tasks.execute_task 落）且信号被清空
        self.assertEqual(self.task.status, 'running')
        self.assertEqual(self.task.pause_action, '')

    def test_pause_abort(self):
        rc, output, results = self._run_with_action('abort')
        self.assertEqual(rc, 1)
        self.assertIn('中止', output)
        self.assertEqual(results[0]['status'], 'failed')

    def test_pause_timeout(self):
        self.step.timeout = 1
        self.step.save()
        # sleep 置空转忙等，1s 超时后返回 124
        with mock.patch.object(runner.time, 'sleep'):
            rc, output, results = runner.run(self.task)
        self.assertEqual(rc, 124)
        self.assertIn('超时', output)

    def test_pause_aborts_following_steps(self):
        TaskStep.objects.create(
            task=self.task, order=2, name='后续命令', task_type='command', command='echo hi',
        )
        rc, output, results = self._run_with_action('abort')
        self.assertEqual(rc, 1)
        self.assertEqual(results[1]['status'], 'skipped')

    def test_confirm_api_requires_paused(self):
        c = self._client()
        r = c.post(f'/api/exec/tasks/{self.task.id}/confirm/', {'action': 'continue'}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('等待处理', r.json()['msg'])

        ExecutionTask.objects.filter(pk=self.task.pk).update(status='paused')
        r = c.post(f'/api/exec/tasks/{self.task.id}/confirm/', {'action': 'bad'}, format='json')
        self.assertEqual(r.status_code, 400)
        r = c.post(f'/api/exec/tasks/{self.task.id}/confirm/', {'action': 'continue'}, format='json')
        self.assertEqual(r.status_code, 200)
        self.task.refresh_from_db()
        self.assertEqual(self.task.pause_action, 'continue')

    def test_confirm_api_requires_admin(self):
        normal = User.objects.create(username='user_p', is_staff=False)
        normal.set_password('Pass!2345xyz')
        normal.save()
        ExecutionTask.objects.filter(pk=self.task.pk).update(status='paused')
        c = self._client('user_p')
        r = c.post(f'/api/exec/tasks/{self.task.id}/confirm/', {'action': 'continue'}, format='json')
        self.assertEqual(r.status_code, 403)

    def test_pause_step_saved_without_content(self):
        """pause 模块保存无需命令/剧本/方法内容。"""
        from apps.asset_mgr.models import Channel, Environment, ProjectGroup

        pg = ProjectGroup.objects.create(name='暂停项目', code='pause-proj')
        ch = Channel.objects.create(name='暂停渠道', code='pause-ch')
        env = Environment.objects.create(name='暂停环境', code='pause-env')
        c = self._client()
        r = c.post('/api/exec/tasks/', {
            'name': '带确认的流',
            'project_group': pg.id, 'channel': ch.id, 'environment': env.id,
            'steps': [
                {'order': 1, 'name': '确认', 'task_type': 'pause', 'timeout': 600},
            ],
        }, format='json')
        self.assertEqual(r.status_code, 201, r.content)


@override_settings(TASK_FAILURE_WAIT_TIMEOUT=0)
class ParallelStageTests(TestCase):
    """模块间并行组：分组规则、并发执行、组内失败中止后续阶段。"""

    def setUp(self):
        self.user = User.objects.create(username='admin_pl', is_staff=True, is_superuser=True)
        self.user.set_password('Pass!2345xyz')
        self.user.save()
        self.task = ExecutionTask.objects.create(name='并行流', created_by=self.user)

    def _step(self, order, name, parallel=False, task_type='command'):
        return TaskStep.objects.create(
            task=self.task, order=order, name=name, task_type=task_type,
            command='echo hi' if task_type == 'command' else '',
            run_parallel=parallel, timeout=30,
        )

    def test_build_stages_grouping(self):
        s1 = self._step(1, '串1')
        s2 = self._step(2, '并A', parallel=True)
        s3 = self._step(3, '并B', parallel=True)
        s4 = self._step(4, '暂停', task_type='pause')
        s5 = self._step(5, '并C', parallel=True)
        stages = runner._build_stages([s1, s2, s3, s4, s5])
        self.assertEqual([[x.name for x in st] for st in stages],
                         [['串1'], ['并A', '并B'], ['暂停'], ['并C']])

    def test_parallel_group_runs_concurrently(self):
        import threading
        self._step(1, '并A', parallel=True)
        self._step(2, '并B', parallel=True)
        barrier = threading.Barrier(2, timeout=10)

        def fake_handler(step, task, servers):
            barrier.wait()  # 两模块必须同时在跑才能都过闸
            return 0, f'{step.name} done'

        with mock.patch.dict(runner._STEP_HANDLERS, {'command': fake_handler}), \
                mock.patch.object(runner, '_step_servers', return_value=[object()]), \
                mock.patch.object(runner, '_guard', return_value=None), \
                mock.patch.object(runner, '_persist_progress'):
            rc, output, results = runner.run(self.task)
        self.assertEqual(rc, 0)
        self.assertEqual([r['status'] for r in results], ['success', 'success'])
        self.assertTrue(all(r['parallel'] for r in results))
        # 输出按模块顺序拼接
        self.assertLess(output.index('并A done'), output.index('并B done'))

    def test_parallel_group_failure_aborts_next_stage(self):
        self._step(1, '并A', parallel=True)
        self._step(2, '并B', parallel=True)
        self._step(3, '串尾')

        def fake_handler(step, task, servers):
            return (1, 'boom') if step.name == '并B' else (0, 'ok')

        with mock.patch.dict(runner._STEP_HANDLERS, {'command': fake_handler}), \
                mock.patch.object(runner, '_step_servers', return_value=[object()]), \
                mock.patch.object(runner, '_guard', return_value=None), \
                mock.patch.object(runner, '_persist_progress'):
            rc, output, results = runner.run(self.task)
        self.assertEqual(rc, 1)
        self.assertEqual([r['status'] for r in results], ['success', 'failed', 'skipped'])
        self.assertIn('中止后续 1 个模块', output)

    def test_pause_step_rejects_run_parallel(self):
        from apps.asset_mgr.models import Channel, Environment, ProjectGroup

        pg = ProjectGroup.objects.create(name='并行项目', code='pl-proj')
        ch = Channel.objects.create(name='并行渠道', code='pl-ch')
        env = Environment.objects.create(name='并行环境', code='pl-env')
        c = APIClient()
        r = c.post('/api/auth/login/', {'username': 'admin_pl', 'password': 'Pass!2345xyz'}, format='json')
        c.credentials(HTTP_AUTHORIZATION='Bearer ' + r.json()['access'])
        r = c.post('/api/exec/tasks/', {
            'name': '非法流',
            'project_group': pg.id, 'channel': ch.id, 'environment': env.id,
            'steps': [{'order': 1, 'name': '确认', 'task_type': 'pause', 'run_parallel': True}],
        }, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('不允许标记并行', r.json()['msg'])


class FailureHandlingTests(TestCase):
    """模块失败处理：暂停等待人工重试/跳过/中止，超时中止，confirm 接口按暂停类型校验动作。"""

    def setUp(self):
        self.user = User.objects.create(username='admin_f', is_staff=True, is_superuser=True)
        self.user.set_password('Pass!2345xyz')
        self.user.save()
        self.task = ExecutionTask.objects.create(name='失败流', created_by=self.user)

    def _client(self, username='admin_f'):
        c = APIClient()
        r = c.post('/api/auth/login/', {'username': username, 'password': 'Pass!2345xyz'}, format='json')
        c.credentials(HTTP_AUTHORIZATION='Bearer ' + r.json()['access'])
        return c

    def _step(self, order, name, parallel=False):
        return TaskStep.objects.create(
            task=self.task, order=order, name=name, task_type='command',
            command='echo hi', run_parallel=parallel, timeout=30,
        )

    def _run(self, handler, actions):
        """执行任务流：handler 为命令模块假执行器；actions 为失败等待期间
        依次注入的处理动作（借 mock 的 sleep 单线程写入，规避 SQLite 跨线程锁）。"""
        queue = list(actions)
        task_pk = self.task.pk

        def _inject(_seconds=None):
            if queue:
                ExecutionTask.objects.filter(pk=task_pk).update(pause_action=queue.pop(0))

        with mock.patch.dict(runner._STEP_HANDLERS, {'command': handler}), \
                mock.patch.object(runner, '_step_servers', return_value=[object()]), \
                mock.patch.object(runner, '_guard', return_value=None), \
                mock.patch.object(runner.time, 'sleep', side_effect=_inject):
            return runner.run(self.task)

    def test_retry_until_success(self):
        """失败后人工重试：当前模块重跑成功，任务流继续且最终成功。"""
        self._step(1, '会失败')
        self._step(2, '后续')
        calls = {'n': 0}

        def handler(step, task, servers):
            if step.name == '会失败':
                calls['n'] += 1
                return (1, 'boom') if calls['n'] == 1 else (0, 'ok after retry')
            return 0, 'tail ok'

        rc, output, results = self._run(handler, ['retry'])
        self.assertEqual(rc, 0)
        self.assertEqual([r['status'] for r in results], ['success', 'success'])
        self.assertEqual(results[0]['attempts'], 2)
        self.assertEqual(results[1]['attempts'], 1)
        self.assertIn('重试#1', output)
        self.assertIn('ok after retry', output)
        self.task.refresh_from_db()
        self.assertEqual(self.task.pause_action, '')
        self.assertEqual(self.task.pause_type, '')

    def test_skip_continues_following_steps(self):
        """失败后人工跳过：后续模块继续执行，失败模块标记 skipped_over，任务最终记失败。"""
        self._step(1, '会失败')
        self._step(2, '后续')

        def handler(step, task, servers):
            return (1, 'boom') if step.name == '会失败' else (0, 'tail ok')

        rc, output, results = self._run(handler, ['skip'])
        self.assertEqual(rc, 1)
        self.assertEqual([r['status'] for r in results], ['failed', 'success'])
        self.assertTrue(results[0].get('skipped_over'))
        self.assertIn('跳过失败模块', output)
        self.assertIn('tail ok', output)

    def test_abort_marks_rest_skipped(self):
        """失败后人工中止：后续模块标记 skipped。"""
        self._step(1, '会失败')
        self._step(2, '后续')

        def handler(step, task, servers):
            return (1, 'boom') if step.name == '会失败' else (0, 'tail ok')

        rc, output, results = self._run(handler, ['abort'])
        self.assertEqual(rc, 1)
        self.assertEqual([r['status'] for r in results], ['failed', 'skipped'])
        self.assertIn('人工确认中止', output)
        self.assertNotIn('tail ok', output)

    def test_wait_timeout_aborts(self):
        """失败等待超时：按中止处理。"""
        self._step(1, '会失败')
        self._step(2, '后续')

        def handler(step, task, servers):
            return (1, 'boom') if step.name == '会失败' else (0, 'tail ok')

        with override_settings(TASK_FAILURE_WAIT_TIMEOUT=1):
            rc, output, results = self._run(handler, [])  # 不注入动作，sleep 空转到超时
        self.assertEqual(rc, 1)
        self.assertEqual([r['status'] for r in results], ['failed', 'skipped'])
        self.assertIn('等待人工处理超时', output)

    def test_retry_parallel_group_only_failed_members(self):
        """并行组失败重试：仅重跑失败模块，成功模块不重复执行。"""
        self._step(1, '并A', parallel=True)
        self._step(2, '并B', parallel=True)
        calls = {'A': 0, 'B': 0}

        def handler(step, task, servers):
            key = 'A' if step.name == '并A' else 'B'
            calls[key] += 1
            if key == 'B' and calls['B'] == 1:
                return 1, 'B boom'
            return 0, f'{step.name} ok'

        rc, output, results = self._run(handler, ['retry'])
        self.assertEqual(rc, 0)
        self.assertEqual([r['status'] for r in results], ['success', 'success'])
        self.assertEqual(calls, {'A': 1, 'B': 2})
        self.assertEqual(results[0]['attempts'], 1)
        self.assertEqual(results[1]['attempts'], 2)

    def test_confirm_api_validates_pause_type(self):
        """confirm 接口：失败等待仅接受 retry/skip/abort，暂停确认仅接受 continue/abort。"""
        c = self._client()
        ExecutionTask.objects.filter(pk=self.task.pk).update(status='paused', pause_type='failed')
        r = c.post(f'/api/exec/tasks/{self.task.id}/confirm/', {'action': 'continue'}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('retry/skip/abort', r.json()['msg'])
        r = c.post(f'/api/exec/tasks/{self.task.id}/confirm/', {'action': 'retry'}, format='json')
        self.assertEqual(r.status_code, 200)
        self.task.refresh_from_db()
        self.assertEqual(self.task.pause_action, 'retry')

        ExecutionTask.objects.filter(pk=self.task.pk).update(
            status='paused', pause_type='confirm', pause_action='')
        r = c.post(f'/api/exec/tasks/{self.task.id}/confirm/', {'action': 'skip'}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('continue/abort', r.json()['msg'])
