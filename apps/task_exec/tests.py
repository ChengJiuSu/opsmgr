"""任务执行 + 平台方法模块测试。

命令/方法执行依赖系统已安装 ansible，通过 local 连接在 127.0.0.1 上运行；
方法执行使用临时 Ansible 项目目录（含带 tags 的剧本）。无 ansible 时相关用例跳过。
"""
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import timedelta
from unittest import mock

import yaml

from channels.db import database_sync_to_async
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.asset_mgr.models import Server
from apps.method_mgr.models import Method

from common import ansible as common_ansible
from common.testing import grant_sudo

from .models import Credential, ExecutionTask, ExecVariable, TaskExecutionLog, TaskStep
from . import live, runner
from .live import PREVIEW_LIMIT, LiveSink
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


def _run_and_capture(client, task_id, payload=None):
    """mock 子进程执行一次任务，返回 (ansible 命令行, 变量文件解析结果)。

    变量以 `-e @vars.yml` 传入且临时目录执行完即删，故在子进程调用时读回。
    """
    captured = {'cmd': [], 'vars': None}

    def fake_run(cmd, *args, **kwargs):
        captured['cmd'] = cmd
        for arg in cmd:
            if arg.startswith('@'):
                with open(arg[1:], encoding='utf-8') as f:
                    captured['vars'] = yaml.safe_load(f.read())
        return 0, 'ok'

    with mock.patch('apps.task_exec.runner.run_stream', side_effect=fake_run):
        client.post(f'/api/exec/tasks/{task_id}/execute/', payload or {}, format='json')
    return captured['cmd'], captured['vars']


class InventoryTests(TestCase):
    def test_local_host_uses_local_connection(self):
        srv = Server.objects.create(name='local', private_ip='127.0.0.1')
        self.assertIn('ansible_connection=local', _inventory_content([srv], None, '/tmp'))

    def test_public_ip_preferred_for_connection(self):
        pub = Server.objects.create(name='pub-01', private_ip='10.0.0.20', public_ip='42.8.8.8')
        nat = Server.objects.create(name='nat-01', private_ip='10.0.0.21')
        content = _inventory_content([pub, nat], None, '/tmp')
        self.assertIn('ansible_host=42.8.8.8', content)
        self.assertNotIn('ansible_host=10.0.0.20', content)
        self.assertIn('ansible_host=10.0.0.21', content)

    def test_credential_password_injected(self):
        srv = Server.objects.create(name='web', private_ip='10.0.0.6')
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
        srv = Server.objects.create(name='game-01', private_ip='10.0.0.8')
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
        """主机名含非法字符时净化为下划线；净化后别名撞车追加 IP 保证唯一。

        主机名 DB 层已全局唯一，但不同主机名净化后仍可能同名（如 web 01 / web+01）。
        """
        s1 = Server.objects.create(name='web 01', private_ip='10.0.0.11')
        s2 = Server.objects.create(name='web+01', private_ip='10.0.0.12')
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
            name='local', private_ip='127.0.0.1',
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

    def _step(self, **overrides):
        """构造模块请求体：非暂停模块服务类型必选，未显式指定时默认基础服务。"""
        step = {'order': 1, 'name': '模块1', 'task_type': 'command', 'command': 'echo hi'}
        step.update(overrides)
        if step.get('task_type') != 'pause' and 'service_type' not in overrides:
            step['service_type'] = self.base_service.id
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
            name='dim-01', private_ip='10.0.1.1', project_group=pg_b, channel=self.ch, environment=self.env,
        )
        ServerService.objects.create(server=in_b, service_type=self.base_service, status='deployed')
        # 未规划服务的主机即使维度匹配也不出现
        Server.objects.create(name='raw-01', private_ip='10.0.1.2', project_group=self.pg, channel=self.ch, environment=self.env)

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

    def test_matched_servers_service_type_filter(self):
        """模块服务类型过滤：跟随全局模块返回命中主机 id 与台数，自定义模块直接收窄匹配集。"""
        from apps.asset_mgr.models import ServerService, ServiceType

        c = self._client()
        gateway = ServiceType.objects.create(name='网关', code='gateway')
        gw_srv = Server.objects.create(
            name='gw-01', private_ip='10.0.3.1',
            project_group=self.pg, channel=self.ch, environment=self.env,
        )
        ServerService.objects.create(server=gw_srv, service_type=gateway, status='deployed')

        task_id = self._create_task(c, steps=[
            self._step(name='仅基础'),
            self._step(order=2, name='仅网关', service_type=gateway.id),
            self._step(order=3, name='自定义仅网关', use_custom_scope=True, service_type=gateway.id,
                       project_group=self.pg.id, channel=self.ch.id, environment=self.env.id),
        ])
        data = c.get(f'/api/exec/tasks/{task_id}/matched_servers/').json()['data']
        self.assertEqual({s['name'] for s in data['global']['results']}, {'local', 'gw-01'})
        # 跟随全局 + 服务类型：台数收窄，命中集供前端与勾选取交集
        self.assertEqual(data['steps'][0]['total'], 1)
        self.assertEqual(data['steps'][0]['server_ids'], [self.server.id])
        self.assertEqual(data['steps'][1]['total'], 1)
        self.assertEqual(data['steps'][1]['server_ids'], [gw_srv.id])
        self.assertEqual(data['steps'][1]['service_type_name'], '网关')
        # 自定义范围 + 服务类型
        self.assertEqual([s['name'] for s in data['steps'][2]['servers']], ['gw-01'])

        # 存量模块（0015 之前、服务类型为空）视为不限：全局全量且不下发命中集
        TaskStep.objects.filter(task_id=task_id, order=1).update(service_type=None)
        data = c.get(f'/api/exec/tasks/{task_id}/matched_servers/').json()['data']
        self.assertEqual(data['steps'][0]['total'], 2)
        self.assertNotIn('server_ids', data['steps'][0])

    def test_service_type_filter_ignores_offline_service(self):
        """服务类型与状态须命中同一条服务记录：该类型已下线的主机不算可执行。"""
        from apps.asset_mgr.models import ServerService, ServiceType

        c = self._client()
        gateway = ServiceType.objects.create(name='网关', code='gateway')
        # local 已规划 base、已下线 gateway → 按 gateway 过滤应为空
        ServerService.objects.create(server=self.server, service_type=gateway, status='offline')
        task_id = self._create_task(c, steps=[self._step(name='仅网关', service_type=gateway.id)])
        data = c.get(f'/api/exec/tasks/{task_id}/matched_servers/').json()['data']
        self.assertEqual(data['steps'][0]['total'], 0)

    def test_step_requires_service_type(self):
        """非暂停模块服务类型必选：未选不予保存。"""
        c = self._client()
        r = c.post('/api/exec/tasks/', {
            'name': 'no-svc', 'project_group': self.pg.id, 'channel': self.ch.id, 'environment': self.env.id,
            'steps': [self._step(service_type=None)],
        }, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('必须选择服务类型', str(r.json()))

    def test_pause_step_rejects_service_type(self):
        c = self._client()
        r = c.post('/api/exec/tasks/', {
            'name': 'bad', 'project_group': self.pg.id, 'channel': self.ch.id, 'environment': self.env.id,
            'steps': [self._step(task_type='pause', command='', service_type=self.base_service.id)],
        }, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('不涉及主机，不能选服务类型', str(r.json()))

    # ── 执行入口校验 ──────────────────────────────────────

    def test_execute_selection_only_affects_global_steps(self):
        """勾选子集仅作用于全局范围模块；自定义范围模块按其维度全量执行。"""
        from apps.asset_mgr.models import ProjectGroup, ServerService

        c = self._client()
        pg_b = ProjectGroup.objects.create(name='项目B', code='proj-b')
        custom_srv = Server.objects.create(
            name='dim-01', private_ip='10.0.1.1', project_group=pg_b, channel=self.ch, environment=self.env,
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
        raw = Server.objects.create(name='raw-02', private_ip='10.0.2.9')  # 未规划
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

    def test_execute_service_type_narrows_and_rejects_empty(self):
        """执行时按模块服务类型收窄主机；过滤后为空则拒绝执行。"""
        from apps.asset_mgr.models import ServerService, ServiceType

        c = self._client()
        gateway = ServiceType.objects.create(name='网关', code='gateway')
        gw_srv = Server.objects.create(
            name='gw-01', private_ip='10.0.3.1',
            project_group=self.pg, channel=self.ch, environment=self.env,
        )
        ServerService.objects.create(server=gw_srv, service_type=gateway, status='planned')
        # gw-01 同时规划基础服务，便于验证「按服务类型收窄」前后的台数差异
        ServerService.objects.create(server=gw_srv, service_type=self.base_service, status='planned')

        task_id = self._create_task(c, steps=[
            self._step(name='全部'),
            self._step(order=2, name='仅网关', service_type=gateway.id),
        ])
        with mock.patch('apps.task_exec.runner.run_stream', return_value=(0, 'ok')):
            r = c.post(f'/api/exec/tasks/{task_id}/execute/', {}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        log = ExecutionTask.objects.get(pk=task_id).logs.get()
        # 未选服务类型的模块跑全局 2 台，选了网关的模块只剩 gw-01
        self.assertEqual([(s['name'], s['server_count']) for s in log.steps_result],
                         [('全部', 2), ('仅网关', 1)])
        self.assertIn('服务类型「网关」', log.output)

        # 服务类型在全局范围内无主机 → 拒绝执行
        empty_type = ServiceType.objects.create(name='数据库', code='db')
        task_id2 = self._create_task(c, steps=[self._step(name='仅DB', service_type=empty_type.id)])
        r = c.post(f'/api/exec/tasks/{task_id2}/execute/', {}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('服务类型「数据库」过滤后为空', str(r.json()))

    def test_execute_step_servers_pick_per_module(self):
        """逐模块勾选：每个跟随全局的模块各跑各自勾选的主机，快照落到模块上。"""
        from apps.asset_mgr.models import ServerService

        c = self._client()
        other = Server.objects.create(
            name='node-02', private_ip='10.0.9.2',
            project_group=self.pg, channel=self.ch, environment=self.env,
        )
        ServerService.objects.create(server=other, service_type=self.base_service, status='planned')

        task_id = self._create_task(c, steps=[
            self._step(name='模块A'),
            self._step(order=2, name='模块B'),
        ])
        task = ExecutionTask.objects.get(pk=task_id)
        step_a, step_b = list(task.steps.all())
        with mock.patch('apps.task_exec.runner.run_stream', return_value=(0, 'ok')):
            r = c.post(f'/api/exec/tasks/{task_id}/execute/', {
                'step_servers': {str(step_a.id): [self.server.id], str(step_b.id): [other.id]},
            }, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual([s.id for s in step_a.servers.all()], [self.server.id])
        self.assertEqual([s.id for s in step_b.servers.all()], [other.id])
        # 任务级快照 = 各模块勾选并集
        self.assertEqual({s.id for s in task.servers.all()}, {self.server.id, other.id})
        log = task.logs.get()
        self.assertEqual([(s['name'], s['server_count']) for s in log.steps_result],
                         [('模块A', 1), ('模块B', 1)])

    def test_execute_rejects_foreign_step_in_step_servers(self):
        c = self._client()
        task_id = self._create_task(c)
        r = c.post(f'/api/exec/tasks/{task_id}/execute/',
                   {'step_servers': {'999999': [self.server.id]}}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('不属于本任务的模块', str(r.json()))

    def test_execute_requires_manual_pick_when_select_all_off(self):
        """关闭默认全选的任务：缺省 server_ids 拒绝执行，显式勾选后放行。"""
        c = self._client()
        task_id = self._create_task(c, default_select_all=False)

        r = c.post(f'/api/exec/tasks/{task_id}/execute/', {}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('人工勾选目标主机', str(r.json()))

        with mock.patch('apps.task_exec.runner.run_stream', return_value=(0, 'ok')):
            r = c.post(f'/api/exec/tasks/{task_id}/execute/',
                       {'server_ids': [self.server.id]}, format='json')
        self.assertEqual(r.status_code, 200, r.content)

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
        with mock.patch('apps.task_exec.trigger.dispatch', return_value=mock.Mock(id=1)):
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
        with mock.patch('apps.task_exec.runner.run_stream', return_value=(0, 'ok')) as m:
            c.post(f'/api/exec/tasks/{task_id}/execute/', {}, format='json')
        cmd = m.call_args[0][0]
        self.assertIn('--forks', cmd)
        self.assertEqual(cmd[cmd.index('--forks') + 1], '1')

        # 并发（默认）：按任务并发数分批，默认 10
        task2 = self._create_task(c)
        with mock.patch('apps.task_exec.runner.run_stream', return_value=(0, 'ok')) as m:
            c.post(f'/api/exec/tasks/{task2}/execute/', {}, format='json')
        cmd = m.call_args[0][0]
        self.assertEqual(cmd[cmd.index('--forks') + 1], '10')

        # 自定义并发数
        task3 = self._create_task(c, forks=25)
        with mock.patch('apps.task_exec.runner.run_stream', return_value=(0, 'ok')) as m:
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
                    'apps.method_mgr.executors.ansible_playbook.run_stream', return_value=(0, 'ok'),
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
        """完整输出落盘，数据库仅存带标注的尾部预览。"""
        from .live import TRUNCATE_NOTE

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

                self.assertTrue(log.output.startswith(TRUNCATE_NOTE))
                self.assertEqual(len(log.output), OUTPUT_PREVIEW_LIMIT + len(TRUNCATE_NOTE))
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
        grant_sudo(c)   # 凭据写操作要求 sudo 二次验证（common/sudo.py）
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

    def test_variables_resolved_endpoint(self):
        """resolved 接口：按维度返回解析合并后的变量字典，语义与执行注入一致。"""
        from apps.asset_mgr.models import Environment

        from .models import ExecVariable

        other_env = Environment.objects.create(name='预发3', code='staging3')
        ExecVariable.objects.create(
            key='app_conf', value='mode: prod\nports:\n  - 80',
            project_group=self.pg, channel=self.ch, environment=self.env,
        )
        ExecVariable.objects.create(
            key='empty_var', value='',
            project_group=self.pg, channel=self.ch, environment=self.env,
        )
        ExecVariable.objects.create(  # 维度不匹配：不返回
            key='other_dim_var', value='x',
            project_group=self.pg, channel=self.ch, environment=other_env,
        )
        ExecVariable.objects.create(  # 停用：不返回
            key='disabled_var', value='x', enabled=False,
            project_group=self.pg, channel=self.ch, environment=self.env,
        )
        c = self._client()
        # 维度按 code 传（外部系统免去查 id）
        r = c.get('/api/exec/variables/resolved/',
                  {'project_group': self.pg.code, 'channel': self.ch.code, 'env': self.env.code})
        self.assertEqual(r.status_code, 200)
        data = r.json()['data']
        self.assertEqual(data['vars'], {'app_conf': {'mode': 'prod', 'ports': [80]}, 'empty_var': ''})
        self.assertEqual(data['count'], 2)
        self.assertEqual(data['dims']['environment']['code'], self.env.code)

        # 维度按 id 传，结果一致
        r = c.get('/api/exec/variables/resolved/',
                  {'project_group': self.pg.id, 'channel': self.ch.id, 'env': self.env.id})
        self.assertEqual(r.json()['data']['vars']['app_conf']['mode'], 'prod')

    def test_variables_resolved_yaml_and_param_validation(self):
        """resolved 接口：output=yaml 返回纯文本；三维度必填、不存在的维度值报错。"""
        from .models import ExecVariable

        ExecVariable.objects.create(
            key='app_conf', value='mode: prod',
            project_group=self.pg, channel=self.ch, environment=self.env,
        )
        c = self._client()
        r = c.get('/api/exec/variables/resolved/', {
            'project_group': self.pg.code, 'channel': self.ch.code,
            'env': self.env.code, 'output': 'yaml',
        })
        self.assertEqual(r.status_code, 200)
        self.assertIn('x-yaml', r['Content-Type'])
        self.assertEqual(yaml.safe_load(r.content.decode()), {'app_conf': {'mode': 'prod'}})

        # 缺维度参数
        r = c.get('/api/exec/variables/resolved/', {'project_group': self.pg.code})
        self.assertEqual(r.status_code, 400)
        self.assertIn('channel', r.json()['msg'])
        # 维度值不存在
        r = c.get('/api/exec/variables/resolved/', {
            'project_group': self.pg.code, 'channel': self.ch.code, 'env': 'no-such-env',
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn('环境不存在', r.json()['msg'])

    def test_variables_resolved_with_artifact(self):
        """resolved 接口：include_artifact=true 附加 package_*，锁定版本不存在则 400。"""
        from apps.artifact_mgr.models import Artifact

        from .models import ExecVariable

        ExecVariable.objects.create(
            key='app_conf', value='mode: prod',
            project_group=self.pg, channel=self.ch, environment=self.env,
        )
        Artifact.objects.create(
            project_group=self.pg, channel=self.ch, environment=self.env,
            version='1.2.3', md5='d' * 32,
            file_path='app-1.2.3.tar.gz', original_name='app-1.2.3.tar.gz', size=1024,
        )
        c = self._client()
        base = {'project_group': self.pg.code, 'channel': self.ch.code, 'env': self.env.code}
        r = c.get('/api/exec/variables/resolved/', dict(base, include_artifact='true'))
        variables = r.json()['data']['vars']
        self.assertEqual(variables['package_version'], '1.2.3')
        self.assertIn('app_conf', variables)
        # 默认不带制品变量
        r = c.get('/api/exec/variables/resolved/', base)
        self.assertNotIn('package_version', r.json()['data']['vars'])
        # 锁定不存在的版本
        r = c.get('/api/exec/variables/resolved/',
                  dict(base, include_artifact='true', artifact_version='9.9.9'))
        self.assertEqual(r.status_code, 400)
        self.assertIn('9.9.9', r.json()['msg'])

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
        """执行注入：仅加载与模块有效维度精确匹配的启用变量，落变量文件以 -e @ 传入。"""
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
        cmd, variables = _run_and_capture(c, task_id)
        idxs = [i for i, a in enumerate(cmd) if a == '--extra-vars']
        self.assertEqual(len(idxs), 2)
        self.assertTrue(cmd[idxs[0] + 1].startswith('@'))
        self.assertEqual(variables, {'app_conf': {'mode': 'prod', 'ports': [80, 443]}})
        self.assertNotIn('other_dim_var', variables)
        self.assertNotIn('disabled_var', variables)
        self.assertEqual(cmd[idxs[1] + 1], 'override=1')  # 模块变量在后，ansible 同名覆盖
        # 变量值不进 argv（同机 ps 可见、且会落入命令行日志）
        self.assertNotIn('suchengjiu', ' '.join(cmd))
        self.assertNotIn('mode', ' '.join(cmd))


@override_settings(TASK_EXEC_BACKEND='sync', TASK_FAILURE_WAIT_TIMEOUT=0)
class ArtifactInjectTests(TestCase):
    """CD 制品变量注入：按模块有效维度取最新制品，params.artifact_version 可锁定版本。"""

    # 复用任务流 API 测试的环境准备与辅助方法（不继承，避免父类用例重复执行）
    setUp = ExecutionAPITests.setUp
    _client = ExecutionAPITests._client
    _step = ExecutionAPITests._step
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
        cmd, _ = _run_and_capture(c, task_id)
        return cmd

    def test_latest_artifact_injected_as_extra_vars(self):
        self._make_artifact('1.0.0')
        latest = self._make_artifact('1.1.0', md5='b' * 32)
        c = self._client()
        cmd, variables = _run_and_capture(c, self._create_task(c))
        idxs = [i for i, a in enumerate(cmd) if a == '--extra-vars']
        self.assertEqual(len(idxs), 1)
        self.assertTrue(cmd[idxs[0] + 1].startswith('@'))  # 走变量文件而非命令行 JSON
        self.assertEqual(variables['package_version'], '1.1.0')
        self.assertEqual(variables['package_md5'], 'b' * 32)
        self.assertIn(f'/api/artifacts/{latest.id}/download/?sig=', variables['package_url'])

    def test_no_artifact_no_injection(self):
        c = self._client()
        cmd = self._run_step_cmd(c, self._create_task(c))
        self.assertNotIn('--extra-vars', cmd)

    def test_locked_version_injected(self):
        target = self._make_artifact('1.0.0')
        self._make_artifact('1.1.0')
        c = self._client()
        task_id = self._create_task(c, steps=[self._step(params={'artifact_version': '1.0.0'})])
        _, variables = _run_and_capture(c, task_id)
        self.assertEqual(variables['package_version'], '1.0.0')
        self.assertIn(f'/api/artifacts/{target.id}/download/', variables['package_url'])

    def test_locked_version_missing_fails_step(self):
        self._make_artifact('1.0.0')
        c = self._client()
        task_id = self._create_task(c, steps=[self._step(params={'artifact_version': '9.9.9'})])
        with mock.patch('apps.task_exec.runner.run_stream', return_value=(0, 'ok')):
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
            name='stage', private_ip='127.0.0.1',
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
        _, variables = _run_and_capture(c, task_id)
        self.assertEqual(variables['package_version'], '2.0.0')
        self.assertIn(f'/api/artifacts/{scoped.id}/download/', variables['package_url'])


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

        def fake_handler(step, task, servers, on_data=None, **kwargs):
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

        def fake_handler(step, task, servers, on_data=None, **kwargs):
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

        def handler(step, task, servers, on_data=None, **kwargs):
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

        def handler(step, task, servers, on_data=None, **kwargs):
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

        def handler(step, task, servers, on_data=None, **kwargs):
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

        def handler(step, task, servers, on_data=None, **kwargs):
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

        def handler(step, task, servers, on_data=None, **kwargs):
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


class RunStreamTests(TestCase):
    """流式子进程执行：分片回调、超时按进程组终止。"""

    def test_streams_chunks_and_returns_full_output(self):
        chunks = []
        rc, out = common_ansible.run_stream(
            [sys.executable, '-c', 'print("first", flush=True); print("second")'],
            timeout=30, on_data=chunks.append,
        )
        self.assertEqual(rc, 0)
        self.assertIn('first', out)
        self.assertIn('second', out)
        # 回调分片拼接后与返回值一致（前端实时视图与最终日志同源）
        self.assertEqual(''.join(chunks), out)

    def test_callback_failure_does_not_break_execution(self):
        def boom(_chunk):
            raise RuntimeError('sink down')

        rc, out = common_ansible.run_stream(
            [sys.executable, '-c', 'print("still ok")'], timeout=30, on_data=boom)
        self.assertEqual(rc, 0)
        self.assertIn('still ok', out)

    def test_timeout_terminates_and_returns_124(self):
        rc, out = common_ansible.run_stream(
            [sys.executable, '-c', 'import time; print("go", flush=True); time.sleep(30)'],
            timeout=1, on_data=lambda _c: None,
        )
        self.assertEqual(rc, 124)
        self.assertIn('go', out)
        self.assertIn('超时', out)


class LiveSinkTests(TestCase):
    """实时输出汇聚：边跑边写库预览，完整日志按模块落盘。"""

    def setUp(self):
        self.task = ExecutionTask.objects.create(name='live')
        self.log = TaskExecutionLog.objects.create(task=self.task)
        self.dir = tempfile.mkdtemp(prefix='live_logs_')
        self.path = os.path.join(self.dir, 'x.log')
        self.sink = LiveSink(self.task.pk, self.log.pk, log_path=self.path)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _db_output(self):
        return ExecutionTask.objects.values_list('output', flat=True).get(pk=self.task.pk)

    def test_running_step_visible_before_finish(self):
        """模块执行中：分片即写库预览（任务与执行日志同步），尚未落盘。"""
        outputs = []
        self.sink.bind(outputs)
        self.sink.begin(('s', 1), '═══ 模块 1/1：跑着 ═══', order=1)
        self.sink.feed(('s', 1), 'partial line\n')
        self.sink.flush(force=True)

        self.assertIn('模块 1/1：跑着', self._db_output())
        self.assertIn('partial line', self._db_output())
        self.assertIn('partial line', TaskExecutionLog.objects.get(pk=self.log.pk).output)
        self.assertFalse(os.path.exists(self.path))  # 落盘按模块粒度，模块未结束

    def test_finished_step_written_to_file_without_duplication(self):
        outputs = []
        self.sink.bind(outputs)
        key = ('s', 1)
        self.sink.begin(key, 'H1', order=1)
        self.sink.feed(key, 'chunk-a\n')
        self.sink.end(key)
        outputs.append('H1\nchunk-a\n')     # runner 结束模块后追加完整块
        self.sink.flush(force=True)
        self.sink.close(final_output='\n\n'.join(outputs))

        with open(self.path, encoding='utf-8') as f:
            content = f.read()
        self.assertEqual(content, 'H1\nchunk-a\n')  # 实时分片不重复落盘

    def test_transient_extra_not_persisted_to_file(self):
        """失败等待提示只进实时预览，不写入落盘日志。"""
        outputs = ['H1\ndone']
        self.sink.bind(outputs)
        self.sink.flush(extra='✖ 等待人工处理…', force=True)
        self.sink.close(final_output='\n\n'.join(outputs))

        self.assertIn('等待人工处理', self._db_output())
        with open(self.path, encoding='utf-8') as f:
            self.assertEqual(f.read(), 'H1\ndone')

    def test_parallel_blocks_ordered_by_step_index(self):
        self.sink.bind([])
        self.sink.begin(('b', 1), 'H2', order=2)
        self.sink.begin(('a', 1), 'H1', order=1)
        self.sink.feed(('b', 1), 'second')
        self.sink.feed(('a', 1), 'first')
        self.sink.flush(force=True)

        out = self._db_output()
        self.assertLess(out.index('first'), out.index('second'))

    def test_file_failure_does_not_break_flush(self):
        """落盘失败（目录不可写等）只告警，写库预览照常。"""
        sink = LiveSink(self.task.pk, self.log.pk, log_path=os.path.join(self.dir, 'x.log', 'nope.log'))
        sink.bind(['H1\ndone'])
        sink.flush(force=True)
        self.assertIn('done', self._db_output())


@override_settings(TASK_EXEC_BACKEND='sync', TASK_FAILURE_WAIT_TIMEOUT=0)
class StreamedTaskLogTests(TestCase):
    """执行链路的流式日志：完整日志落盘 + output 接口增量拉取。"""

    setUp = ExecutionAPITests.setUp
    _client = ExecutionAPITests._client
    _step = ExecutionAPITests._step
    _create_task = ExecutionAPITests._create_task

    def test_full_log_always_persisted_to_file(self):
        if not ANSIBLE:
            self.skipTest('ansible 未安装')
        log_dir = tempfile.mkdtemp(prefix='task_logs_')
        try:
            with override_settings(TASK_LOG_DIR=log_dir):
                c = self._client()
                task_id = self._create_task(c, steps=[self._step(command='echo stream_marker')])
                c.post(f'/api/exec/tasks/{task_id}/execute/', {}, format='json')
                log = TaskExecutionLog.objects.get(task_id=task_id)
                # 输出未超预览上限也落盘（历史行为仅超长才落盘）
                self.assertTrue(log.log_file)
                with open(os.path.join(log_dir, log.log_file), encoding='utf-8') as f:
                    content = f.read()
                self.assertIn('stream_marker', content)
                self.assertIn('模块 1/1', content)
                # 详情接口读落盘全量
                r = c.get(f'/api/exec/tasks/{task_id}/logs/{log.id}/')
                self.assertEqual(r.json()['data']['output'], content)
        finally:
            shutil.rmtree(log_dir, ignore_errors=True)

    def test_playbook_output_visible_before_step_finishes(self):
        """流式：剧本第一个 task 的输出在模块跑完前就已可见（写库预览）。"""
        if not ANSIBLE:
            self.skipTest('ansible 未安装')
        proj = tempfile.mkdtemp(prefix='ansible_proj_')
        with open(os.path.join(proj, 'slow.yml'), 'w') as f:
            f.write(
                '- hosts: all\n'
                '  gather_facts: false\n'
                '  tasks:\n'
                '    - debug: {msg: "first_task_marker"}\n'
                '    - command: sleep 2\n'
                '    - debug: {msg: "second_task_marker"}\n'
            )
        snapshots = []
        original = LiveSink._save_preview

        def spy(sink_self, preview):
            snapshots.append(preview)
            original(sink_self, preview)

        try:
            with override_settings(ANSIBLE_PROJECT_DIR=proj, TASK_LOG_DIR=proj):
                task = ExecutionTask.objects.create(name='slow', created_by=self.admin)
                TaskStep.objects.create(
                    task=task, order=1, name='慢剧本', task_type='playbook',
                    playbook='slow.yml', timeout=60,
                )
                task.servers.add(self.server)
                from .tasks import dispatch
                with mock.patch.object(LiveSink, '_save_preview', spy):
                    dispatch(task, triggered_by=self.admin)
            task.refresh_from_db()
            self.assertEqual(task.status, 'success')
            self.assertIn('second_task_marker', task.output)
            # 关键：存在「已见第一个 task、尚未见最后一个 task」的中间快照
            self.assertTrue(any(
                'first_task_marker' in s and 'second_task_marker' not in s for s in snapshots),
                '未捕获到流式中间输出（日志仍是模块跑完才落库）')
        finally:
            shutil.rmtree(proj, ignore_errors=True)

    def test_output_api_incremental(self):
        c = self._client()
        task = ExecutionTask.objects.create(name='inc', created_by=self.admin, status='running')
        ExecutionTask.objects.filter(pk=task.pk).update(output='0123456789')

        r = c.get(f'/api/exec/tasks/{task.id}/output/', {'offset': 4})
        data = r.json()['data']
        self.assertTrue(data['delta'])
        self.assertEqual(data['output'], '456789')
        self.assertEqual(data['offset'], 10)

        # offset 越界（如日志被重置）回退整段
        r = c.get(f'/api/exec/tasks/{task.id}/output/', {'offset': 99})
        data = r.json()['data']
        self.assertFalse(data['delta'])
        self.assertEqual(data['output'], '0123456789')

        # 无 offset 与非法 offset 均为整段
        for params in ({}, {'offset': 'abc'}):
            data = c.get(f'/api/exec/tasks/{task.id}/output/', params).json()['data']
            self.assertFalse(data['delta'])
            self.assertEqual(data['output'], '0123456789')

    def test_output_api_full_when_preview_saturated(self):
        """预览已达上限（尾部截断，前缀漂移）时禁用增量，回退整段。"""
        c = self._client()
        task = ExecutionTask.objects.create(name='sat', created_by=self.admin, status='running')
        ExecutionTask.objects.filter(pk=task.pk).update(output='y' * (PREVIEW_LIMIT + 10))
        data = c.get(f'/api/exec/tasks/{task.id}/output/', {'offset': 5}).json()['data']
        self.assertFalse(data['delta'])
        self.assertEqual(len(data['output']), PREVIEW_LIMIT + 10)


@override_settings(TASK_EXEC_BACKEND='sync', TASK_FAILURE_WAIT_TIMEOUT=0)
class ExecSceneArchiveTests(TestCase):
    """执行现场归档：inventory/变量/命令随执行记录落盘，供事后 debug 复现。"""

    setUp = ExecutionAPITests.setUp
    _client = ExecutionAPITests._client
    _step = ExecutionAPITests._step
    _create_task = ExecutionAPITests._create_task

    def _scene_dir(self, log_dir, task_id, sub='step1/direct'):
        log = TaskExecutionLog.objects.get(task_id=task_id)
        return os.path.join(log_dir, str(task_id), f'{log.id}.d', sub)

    def test_scene_archived_for_direct_run(self):
        from .models import Credential, ExecVariable

        ExecVariable.objects.create(
            key='app_conf', value='mode: prod',
            project_group=self.pg, channel=self.ch, environment=self.env,
        )
        credential = Credential.objects.create(
            name='ut-cred', username='deploy', auth_type='password', password='s3cr3t')
        # 远端主机才会带连接凭据（本机走 ansible_connection=local）
        from apps.asset_mgr.models import ServerService

        remote = Server.objects.create(
            name='remote', private_ip='10.0.0.9', ssh_port=2222,
            project_group=self.pg, channel=self.ch, environment=self.env,
        )
        ServerService.objects.create(
            server=remote, service_type=self.base_service, status='planned')
        log_dir = tempfile.mkdtemp(prefix='task_logs_')
        try:
            with override_settings(TASK_LOG_DIR=log_dir):
                c = self._client()
                task_id = self._create_task(c, credential=credential.id)
                _run_and_capture(c, task_id)
                scene = self._scene_dir(log_dir, task_id)
                with open(os.path.join(scene, 'vars.yml'), encoding='utf-8') as f:
                    self.assertEqual(yaml.safe_load(f.read()), {'app_conf': {'mode': 'prod'}})
                with open(os.path.join(scene, 'inventory.ini'), encoding='utf-8') as f:
                    inventory = f.read()
                with open(os.path.join(scene, 'command.txt'), encoding='utf-8') as f:
                    command = f.read()
        finally:
            shutil.rmtree(log_dir, ignore_errors=True)

        # 归档 inventory 为无密钥版本：主机与连接参数在，密码不落盘
        self.assertIn('ansible_user=deploy', inventory)
        self.assertNotIn('s3cr3t', inventory)
        # 命令内的临时目录路径改写为归档副本，复制即可重跑
        self.assertIn(os.path.join(scene, 'inventory.ini'), command)
        self.assertIn('@' + os.path.join(scene, 'vars.yml'), command)
        self.assertNotIn('/tmp/ops_cmd_', command)

    def test_scene_api_lists_and_reads_files(self):
        from .models import ExecVariable

        ExecVariable.objects.create(
            key='app_conf', value='mode: prod',
            project_group=self.pg, channel=self.ch, environment=self.env,
        )
        log_dir = tempfile.mkdtemp(prefix='task_logs_')
        try:
            with override_settings(TASK_LOG_DIR=log_dir):
                c = self._client()
                task_id = self._create_task(c)
                _run_and_capture(c, task_id)
                log = TaskExecutionLog.objects.get(task_id=task_id)
                base = f'/api/exec/tasks/{task_id}/logs/{log.id}/scene/'

                r = c.get(base)
                self.assertEqual(r.status_code, 200)
                paths = [f['path'] for f in r.json()['data']['files']]
                self.assertIn(os.path.join('step1', 'direct', 'vars.yml'), paths)
                self.assertIn(os.path.join('step1', 'direct', 'inventory.ini'), paths)
                self.assertIn(os.path.join('step1', 'direct', 'command.txt'), paths)

                r = c.get(base, {'file': os.path.join('step1', 'direct', 'vars.yml')})
                self.assertEqual(
                    yaml.safe_load(r.json()['data']['content']), {'app_conf': {'mode': 'prod'}})
                # 路径穿越拒绝
                r = c.get(base, {'file': '../../../../etc/passwd'})
                self.assertEqual(r.status_code, 400)
                # 不存在的文件
                r = c.get(base, {'file': 'step9/direct/vars.yml'})
                self.assertEqual(r.status_code, 400)

                # 归档与执行日志同档（exec read）：敏感变量落盘时已打码、
                # inventory 是无密钥版本，且「日志文件」页读的就是同一批文件
                u = User.objects.create(username='scene_viewer')
                u.set_password('Pass!2345xyz')
                u.save()
                vc = APIClient()
                login = vc.post('/api/auth/login/',
                                {'username': 'scene_viewer', 'password': 'Pass!2345xyz'},
                                format='json')
                vc.credentials(HTTP_AUTHORIZATION='Bearer ' + login.json()['access'])
                self.assertEqual(vc.get(base).status_code, 200)

                # exec=none 的角色仍然拒
                from apps.auth_mgr.models import Role

                blocked_role = Role.objects.create(
                    name='无执行权限', code='scene_none', permissions={'exec': 'none'})
                blocked = User.objects.create(username='scene_blocked', role=blocked_role)
                blocked.set_password('Pass!2345xyz')
                blocked.save()
                bc = APIClient()
                login = bc.post('/api/auth/login/',
                                {'username': 'scene_blocked', 'password': 'Pass!2345xyz'},
                                format='json')
                bc.credentials(HTTP_AUTHORIZATION='Bearer ' + login.json()['access'])
                self.assertEqual(bc.get(base).status_code, 403)
        finally:
            shutil.rmtree(log_dir, ignore_errors=True)

    def test_scene_api_empty_after_cleanup(self):
        """归档过保留期被清理后清单为空，接口不报错。"""
        log_dir = tempfile.mkdtemp(prefix='task_logs_')
        try:
            with override_settings(TASK_LOG_DIR=log_dir):
                c = self._client()
                task_id = self._create_task(c)
                _run_and_capture(c, task_id)
                log = TaskExecutionLog.objects.get(task_id=task_id)
                shutil.rmtree(os.path.join(log_dir, str(task_id), f'{log.id}.d'))
                r = c.get(f'/api/exec/tasks/{task_id}/logs/{log.id}/scene/')
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json()['data'], {'files': [], 'retained': False})
        finally:
            shutil.rmtree(log_dir, ignore_errors=True)

    def test_no_archive_without_log_binding(self):
        """直接调 runner.run（无执行记录，如单测/预演）不归档，也不报错。"""
        from .runner import step_archive_dir

        task = ExecutionTask.objects.create(name='no-log')
        step = TaskStep.objects.create(
            task=task, order=1, name='cmd', task_type='command', command='echo')
        self.assertIsNone(step_archive_dir(LiveSink(task.pk), step))


class AgentPayloadVarsTests(TestCase):
    """agent 下发 payload 的变量字段：新旧双发，内容等价。"""

    setUp = ExecutionAPITests.setUp

    def test_payload_carries_vars_file_and_legacy_list(self):
        from .models import ExecVariable
        from .runner import _agent_payload_base

        ExecVariable.objects.create(
            key='app_conf', value='mode: prod',
            project_group=self.pg, channel=self.ch, environment=self.env,
        )
        task = ExecutionTask.objects.create(
            name='agent', project_group=self.pg, channel=self.ch, environment=self.env)
        step = TaskStep.objects.create(
            task=task, order=1, name='cmd', task_type='command',
            command='echo', extra_vars='override=1')

        payload = _agent_payload_base(step, task, [self.server])
        # 新字段：变量文件文本 + 模块额外变量分离
        self.assertEqual(yaml.safe_load(payload['vars_file']), {'app_conf': {'mode': 'prod'}})
        self.assertEqual(payload['step_extra_vars'], 'override=1')
        # 旧字段：逐个 -e 的值列表，效果等价（旧版 agent 不认新字段时兜底）
        self.assertEqual(len(payload['extra_vars']), 2)
        self.assertEqual(json.loads(payload['extra_vars'][0]), {'app_conf': {'mode': 'prod'}})
        self.assertEqual(payload['extra_vars'][1], 'override=1')
        # inventory 下发无密钥版本
        self.assertNotIn('ansible_ssh_pass', payload['inventory'])

    def test_payload_vars_empty_when_no_variables(self):
        from .runner import _agent_payload_base

        task = ExecutionTask.objects.create(
            name='agent2', project_group=self.pg, channel=self.ch, environment=self.env)
        step = TaskStep.objects.create(
            task=task, order=1, name='cmd', task_type='command', command='echo')
        payload = _agent_payload_base(step, task, [self.server])
        self.assertEqual(payload['vars_file'], '')
        self.assertEqual(payload['step_extra_vars'], '')
        self.assertEqual(payload['extra_vars'], [])


class CleanTaskLogFilesTests(TestCase):
    """落盘日志清理：按 mtime 清过期文件（含孤儿），记录按各自保留期清。"""

    def setUp(self):
        self.task = ExecutionTask.objects.create(name='cleanup')
        self.dir = tempfile.mkdtemp(prefix='task_logs_')

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write(self, rel, days_old):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write('log body')
        old = time.time() - days_old * 86400
        os.utime(path, (old, old))
        return path

    def test_files_only_removes_expired_and_clears_reference(self):
        fresh_log = TaskExecutionLog.objects.create(task=self.task, log_file='1/fresh.log', output='预览')
        old_log = TaskExecutionLog.objects.create(task=self.task, log_file='1/old.log', output='预览')
        fresh = self._write('1/fresh.log', 3)
        old = self._write('1/old.log', 45)
        orphan = self._write('1/orphan.log', 45)  # 记录已删、文件残留

        with override_settings(TASK_LOG_DIR=self.dir, TASK_LOG_FILE_RETENTION_DAYS=30):
            call_command('clean_task_logs', '--files-only')

        self.assertTrue(os.path.exists(fresh))
        self.assertFalse(os.path.exists(old))
        self.assertFalse(os.path.exists(orphan))
        # 文件已清的记录仍在，log_file 置空后详情页退回数据库预览
        old_log.refresh_from_db()
        fresh_log.refresh_from_db()
        self.assertEqual(old_log.log_file, '')
        self.assertEqual(fresh_log.log_file, '1/fresh.log')
        self.assertEqual(TaskExecutionLog.objects.count(), 2)

    def test_expired_records_deleted_with_their_files(self):
        old_log = TaskExecutionLog.objects.create(task=self.task, log_file='1/rec.log')
        TaskExecutionLog.objects.filter(pk=old_log.pk).update(
            created_at=timezone.now() - timedelta(days=120))
        path = self._write('1/rec.log', 1)  # 文件本身未过期，随记录一起清

        with override_settings(TASK_LOG_DIR=self.dir, TASK_LOG_RETENTION_DAYS=90,
                               TASK_LOG_FILE_RETENTION_DAYS=30):
            call_command('clean_task_logs')

        self.assertFalse(os.path.exists(path))
        self.assertFalse(TaskExecutionLog.objects.filter(pk=old_log.pk).exists())

    def _write_scene(self, rel, days_old):
        """造一份执行现场归档：<task>/<log>.d/step1/direct/{inventory.ini,vars.yml}。"""
        scene = os.path.join(self.dir, rel, 'step1', 'direct')
        os.makedirs(scene, exist_ok=True)
        old = time.time() - days_old * 86400
        for name in ('inventory.ini', 'vars.yml'):
            path = os.path.join(scene, name)
            with open(path, 'w', encoding='utf-8') as f:
                f.write('scene')
            os.utime(path, (old, old))
        return os.path.join(self.dir, rel)

    def test_expired_scene_dirs_removed(self):
        """归档目录与落盘日志同保留期：过期按 mtime 清，未过期保留。"""
        fresh = self._write_scene('1/10.d', 3)
        old = self._write_scene('1/11.d', 45)
        with override_settings(TASK_LOG_DIR=self.dir, TASK_LOG_FILE_RETENTION_DAYS=30):
            call_command('clean_task_logs', '--files-only')
        self.assertTrue(os.path.isdir(fresh))
        self.assertFalse(os.path.exists(old))

    def test_scene_dir_removed_with_record(self):
        """记录过期删除时带走归档目录（log_file 为空的记录也有归档）。"""
        log = TaskExecutionLog.objects.create(task=self.task)
        TaskExecutionLog.objects.filter(pk=log.pk).update(
            created_at=timezone.now() - timedelta(days=120))
        scene = self._write_scene(f'{self.task.pk}/{log.pk}.d', 1)  # 未过期，随记录一起清
        with override_settings(TASK_LOG_DIR=self.dir, TASK_LOG_RETENTION_DAYS=90,
                               TASK_LOG_FILE_RETENTION_DAYS=30):
            call_command('clean_task_logs')
        self.assertFalse(os.path.exists(scene))
        self.assertFalse(TaskExecutionLog.objects.filter(pk=log.pk).exists())

    def test_dry_run_keeps_everything(self):
        old_log = TaskExecutionLog.objects.create(task=self.task, log_file='1/old.log')
        path = self._write('1/old.log', 45)
        with override_settings(TASK_LOG_DIR=self.dir, TASK_LOG_FILE_RETENTION_DAYS=30):
            call_command('clean_task_logs', '--dry-run')
        self.assertTrue(os.path.exists(path))
        old_log.refresh_from_db()
        self.assertEqual(old_log.log_file, '1/old.log')


IN_MEMORY_LAYER = {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}}


@override_settings(CHANNEL_LAYERS=IN_MEMORY_LAYER)
class TaskLogConsumerTests(TransactionTestCase):
    """日志推送 WebSocket：子协议鉴权、连接快照、组消息下行。

    TransactionTestCase：consumer 的 database_sync_to_async 在其他线程读库，
    TestCase 的事务隔离会挡住可见性。
    """

    def setUp(self):
        from .routing import websocket_urlpatterns

        self.user = User.objects.create(username='ws_user', is_active=True)
        self.user.set_password('Pass!2345xyz')
        self.user.save()
        self.task = ExecutionTask.objects.create(name='ws-task', status='running', output='已有输出')
        self.app = URLRouter(websocket_urlpatterns)

    def _token(self):
        from rest_framework_simplejwt.tokens import AccessToken

        return str(AccessToken.for_user(self.user))

    def _communicator(self, token=None, task_id=None, use_query=False):
        task_id = task_id or self.task.pk
        token = self._token() if token is None else token
        path = f'/ws/task-logs/{task_id}/'
        if use_query:
            return WebsocketCommunicator(self.app, f'{path}?token={token}')
        return WebsocketCommunicator(self.app, path, subprotocols=['jwt', token])

    async def test_snapshot_then_delta_push(self):
        from .live import push, push_status

        comm = self._communicator()
        connected, subprotocol = await comm.connect()
        self.assertTrue(connected)
        self.assertEqual(subprotocol, 'jwt')

        snapshot = await comm.receive_json_from()
        self.assertEqual(snapshot['kind'], 'full')
        self.assertEqual(snapshot['output'], '已有输出')
        self.assertEqual(snapshot['status'], 'running')

        # LiveSink/tasks 侧的推送经组消息下行
        await database_sync_to_async(push)(
            self.task.pk, {'kind': 'delta', 'output': '新增一行\n', 'offset': 12})
        delta = await comm.receive_json_from()
        self.assertEqual(delta, {'kind': 'delta', 'output': '新增一行\n', 'offset': 12})

        await database_sync_to_async(
            lambda: ExecutionTask.objects.filter(pk=self.task.pk).update(
                status='success', return_code=0))()
        await database_sync_to_async(push_status)(self.task.pk)
        status = await comm.receive_json_from()
        self.assertEqual(status['kind'], 'status')
        self.assertEqual(status['status'], 'success')
        self.assertEqual(status['return_code'], 0)

        await comm.disconnect()

    async def test_token_via_query_param_also_accepted(self):
        comm = self._communicator(use_query=True)
        connected, _ = await comm.connect()
        self.assertTrue(connected)
        self.assertEqual((await comm.receive_json_from())['kind'], 'full')
        await comm.disconnect()

    async def test_reject_without_token(self):
        comm = WebsocketCommunicator(self.app, f'/ws/task-logs/{self.task.pk}/')
        connected, _ = await comm.connect()
        if connected:
            msg = await comm.receive_output()
            self.assertEqual(msg['type'], 'websocket.close')
            self.assertEqual(msg['code'], 4401)
        await comm.disconnect()

    async def test_reject_bad_token(self):
        comm = self._communicator(token='not-a-jwt')
        connected, _ = await comm.connect()
        if connected:
            msg = await comm.receive_output()
            self.assertEqual(msg['code'], 4401)
        await comm.disconnect()

    async def test_missing_task_closed(self):
        comm = self._communicator(task_id=999999)
        connected, _ = await comm.connect()
        if connected:
            msg = await comm.receive_output()
            self.assertEqual(msg['code'], 4404)
        await comm.disconnect()

    async def test_channel_layer_unavailable_rejects_handshake(self):
        """通道不可用（未配/连不上 Redis）：直接拒握手，前端据此回退轮询。"""
        comm = self._communicator()
        with mock.patch('channels.layers.InMemoryChannelLayer.group_add',
                        side_effect=RuntimeError('redis 不可达')):
            connected, code = await comm.connect()
        self.assertFalse(connected)
        self.assertEqual(code, 4503)
        await comm.disconnect()

    async def test_ping_keepalive(self):
        comm = self._communicator()
        await comm.connect()
        await comm.receive_json_from()  # 快照
        await comm.send_json_to({'type': 'ping'})
        self.assertEqual((await comm.receive_json_from())['kind'], 'pong')
        await comm.disconnect()


@override_settings(CHANNEL_LAYERS=IN_MEMORY_LAYER)
class LiveSinkBroadcastTests(TestCase):
    """LiveSink 推送语义：追加发 delta，前缀被破坏发整段。"""

    def setUp(self):
        self.task = ExecutionTask.objects.create(name='bc')
        self.sink = LiveSink(self.task.pk)
        self.sink.bind([])
        self.pushes = []

    def _patch_push(self):
        return mock.patch('apps.task_exec.live.push',
                          side_effect=lambda _tid, payload: self.pushes.append(payload) or True)

    def test_append_pushes_delta(self):
        with self._patch_push():
            self.sink.begin(('s', 1), 'H1', order=1)
            self.sink.feed(('s', 1), 'aaa')
            self.sink.flush(force=True)
            self.sink.feed(('s', 1), 'bbb')
            self.sink.flush(force=True)
        # 全程按追加推送（客户端连接时已拿到快照，delta 接在其后）
        self.assertTrue(all(p['kind'] == 'delta' for p in self.pushes))
        self.assertEqual([p['output'] for p in self.pushes], ['H1\n', 'aaa', 'bbb'])

    def test_transient_extra_and_parallel_push_full(self):
        with self._patch_push():
            self.sink.begin(('a', 1), 'H1', order=1)
            self.sink.flush(force=True)
            self.sink.flush(extra='等待人工处理…', force=True)      # 临时提示 → 整段
            self.sink.begin(('b', 1), 'H2', order=2)                # 并行组 → 整段
            self.sink.feed(('b', 1), 'x')
            self.sink.flush(force=True)
        self.assertTrue(all(p['kind'] == 'full' for p in self.pushes[1:]))
        self.assertIn('等待人工处理', self.pushes[1]['output'])

    def test_broken_channel_stops_pushing(self):
        with mock.patch('apps.task_exec.live.push', return_value=False) as m:
            self.sink.begin(('s', 1), 'H1', order=1)
            self.sink.feed(('s', 1), 'aaa')
            self.sink.flush(force=True)
            self.sink.flush(force=True)
        self.assertEqual(m.call_count, 1)  # 首次失败后不再重试


class LogFileBrowserTests(TestCase):
    """日志文件浏览接口：目录列表 / 文本读取 / 下载 / 路径穿越与权限。

    不依赖真实执行：直接在临时 TASK_LOG_DIR 内造出与 runner 一致的目录结构。
    """

    def setUp(self):
        self.admin = User.objects.create(username='logadmin', is_staff=True, is_superuser=True)
        self.admin.set_password('Pass!2345xyz')
        self.admin.save()
        self.dir = tempfile.mkdtemp(prefix='task_logs_')
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.task = ExecutionTask.objects.create(name='部署任务')
        self.log = TaskExecutionLog.objects.create(
            task=self.task, status='success', started_at=timezone.now())
        task_dir = os.path.join(self.dir, str(self.task.id))
        scene = os.path.join(task_dir, f'{self.log.id}.d', 'step1', 'direct')
        os.makedirs(scene)
        self._write(os.path.join(task_dir, f'{self.log.id}.log'), 'ok: [local]\n' * 5)
        self._write(os.path.join(scene, 'inventory.ini'), 'local ansible_host=127.0.0.1\n')
        self._write(os.path.join(scene, 'vars.yml'), 'app_conf:\n  mode: prod\n')
        self._write(os.path.join(scene, 'command.txt'), 'ansible all -m shell -a "echo hi"\n')

    @staticmethod
    def _write(path, text):
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)

    def _client(self, username='logadmin'):
        c = APIClient()
        r = c.post('/api/auth/login/',
                   {'username': username, 'password': 'Pass!2345xyz'}, format='json')
        c.credentials(HTTP_AUTHORIZATION='Bearer ' + r.json()['access'])
        return c

    def test_list_root_annotates_task_name(self):
        with override_settings(TASK_LOG_DIR=self.dir):
            r = self._client().get('/api/exec/log-files/')
        self.assertEqual(r.status_code, 200)
        data = r.json()['data']
        self.assertIsNone(data['parent'])
        entry = next(e for e in data['entries'] if e['name'] == str(self.task.id))
        self.assertEqual(entry['type'], 'dir')
        self.assertEqual(entry['label'], '部署任务')

    def test_list_task_dir_annotates_log_status(self):
        with override_settings(TASK_LOG_DIR=self.dir):
            r = self._client().get('/api/exec/log-files/', {'path': str(self.task.id)})
        data = r.json()['data']
        self.assertEqual(data['parent'], '')
        labels = {e['name']: e['label'] for e in data['entries']}
        self.assertIn('执行现场', labels[f'{self.log.id}.d'])
        self.assertIn('执行日志 · 成功', labels[f'{self.log.id}.log'])
        # 同一执行记录的归档目录与日志文件相邻，目录在前
        self.assertEqual([e['name'] for e in data['entries']],
                         [f'{self.log.id}.d', f'{self.log.id}.log'])

    def test_list_nested_scene_dir(self):
        rel = f'{self.task.id}/{self.log.id}.d/step1/direct'
        with override_settings(TASK_LOG_DIR=self.dir):
            r = self._client().get('/api/exec/log-files/', {'path': rel})
        data = r.json()['data']
        self.assertEqual(data['parent'], f'{self.task.id}/{self.log.id}.d/step1')
        names = {e['name'] for e in data['entries']}
        self.assertEqual(names, {'inventory.ini', 'vars.yml', 'command.txt'})
        self.assertTrue(all(e['size'] > 0 for e in data['entries']))

    def test_read_file_content(self):
        rel = f'{self.task.id}/{self.log.id}.d/step1/direct/vars.yml'
        with override_settings(TASK_LOG_DIR=self.dir):
            r = self._client().get('/api/exec/log-files/content/', {'path': rel})
        data = r.json()['data']
        self.assertEqual(yaml.safe_load(data['content']), {'app_conf': {'mode': 'prod'}})
        self.assertTrue(data['eof'])

    def test_read_tail_and_offset(self):
        rel = f'{self.task.id}/{self.log.id}.log'
        with override_settings(TASK_LOG_DIR=self.dir):
            c = self._client()
            r = c.get('/api/exec/log-files/content/', {'path': rel, 'tail': 'true', 'limit': 12})
            tail = r.json()['data']
            r = c.get('/api/exec/log-files/content/', {'path': rel, 'offset': 12, 'limit': 12})
            mid = r.json()['data']
        self.assertEqual(tail['content'], 'ok: [local]\n')
        self.assertEqual(tail['offset'], tail['size'] - 12)
        self.assertTrue(tail['eof'])
        self.assertEqual(mid['offset'], 12)
        self.assertFalse(mid['eof'])

    def test_download_file(self):
        rel = f'{self.task.id}/{self.log.id}.d/step1/direct/command.txt'
        with override_settings(TASK_LOG_DIR=self.dir):
            r = self._client().get('/api/exec/log-files/download/', {'path': rel})
        self.assertEqual(r.status_code, 200)
        self.assertIn('attachment', r['Content-Disposition'])
        self.assertIn(b'echo hi', b''.join(r.streaming_content))

    def test_path_traversal_rejected(self):
        with override_settings(TASK_LOG_DIR=self.dir):
            c = self._client()
            for params in ({'path': '../../../../etc'}, {'path': f'{self.task.id}/../../etc'}):
                self.assertEqual(c.get('/api/exec/log-files/', params).status_code, 400)
            r = c.get('/api/exec/log-files/content/', {'path': '../../../../etc/passwd'})
            self.assertEqual(r.status_code, 400)
            r = c.get('/api/exec/log-files/download/', {'path': '../../../../etc/passwd'})
            self.assertEqual(r.status_code, 400)

    def test_read_level_can_browse_but_none_cannot(self):
        """exec 只读档能翻日志；exec=none 的角色仍然一律拒。"""
        from apps.auth_mgr.models import Role

        reader_role = Role.objects.create(
            name='日志只读', code='log_reader', permissions={'exec': 'read'})
        reader = User.objects.create(username='logreader', role=reader_role)
        reader.set_password('Pass!2345xyz')
        reader.save()
        nobody_role = Role.objects.create(
            name='无执行权限', code='no_exec', permissions={'exec': 'none'})
        nobody = User.objects.create(username='lognobody', role=nobody_role)
        nobody.set_password('Pass!2345xyz')
        nobody.save()
        rel = f'{self.task.id}/{self.log.id}.log'
        with override_settings(TASK_LOG_DIR=self.dir):
            self.assertEqual(
                self._client('logreader').get(
                    '/api/exec/log-files/content/', {'path': rel}).status_code, 200)
            self.assertEqual(
                self._client('lognobody').get('/api/exec/log-files/').status_code, 403)

    def test_symlink_outside_root_rejected(self):
        """指向根目录外的软链接同样拒绝（realpath 比较）。"""
        outside = tempfile.mkdtemp(prefix='outside_')
        self.addCleanup(shutil.rmtree, outside, True)
        self._write(os.path.join(outside, 'secret.txt'), 'top secret')
        link = os.path.join(self.dir, 'escape.txt')
        os.symlink(os.path.join(outside, 'secret.txt'), link)
        with override_settings(TASK_LOG_DIR=self.dir):
            r = self._client().get('/api/exec/log-files/content/', {'path': 'escape.txt'})
        self.assertEqual(r.status_code, 400)

    def test_missing_path_and_root(self):
        with override_settings(TASK_LOG_DIR=self.dir):
            c = self._client()
            self.assertEqual(c.get('/api/exec/log-files/', {'path': 'nope'}).status_code, 400)
            self.assertEqual(c.get('/api/exec/log-files/content/').status_code, 400)
        # 根目录尚未创建（从未执行过任务）时返回空列表而非报错
        with override_settings(TASK_LOG_DIR=os.path.join(self.dir, 'not-created')):
            r = self._client().get('/api/exec/log-files/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['data']['entries'], [])

    def test_no_role_member_can_read(self):
        """无角色的非 staff 走回退矩阵（exec=read），能翻日志。

        原来这三个接口要 exec 写档，这条用例断言的是「非管理员一律 403」。
        日志正文与 logs/{id}/ 详情返回的是同一份内容，拦它拦不住什么，只是把
        「看自己跑过的日志」变成管理员专属——新人排障恰恰最需要翻这些。
        """
        u = User.objects.create(username='logviewer')
        u.set_password('Pass!2345xyz')
        u.save()
        rel = f'{self.task.id}/{self.log.id}.log'
        with override_settings(TASK_LOG_DIR=self.dir):
            c = self._client('logviewer')
            self.assertEqual(c.get('/api/exec/log-files/').status_code, 200)
            self.assertEqual(
                c.get('/api/exec/log-files/content/', {'path': rel}).status_code, 200)
            self.assertEqual(
                c.get('/api/exec/log-files/download/', {'path': rel}).status_code, 200)


@override_settings(TRUSTED_PROXIES=['127.0.0.1'], TASK_EXEC_BACKEND='sync',
                   TASK_FAILURE_WAIT_TIMEOUT=0)
class TaskTriggerApiTests(TestCase):
    """接口/Webhook 触发任务流：凭证鉴权 + 业务约束（默认全选、生产确认）。"""

    ALLOWED_IP = '203.0.113.9'

    def setUp(self):
        from apps.asset_mgr.models import Channel, Environment, ProjectGroup, ServerService, ServiceType
        from apps.auth_mgr.apikey import ensure_service_user
        from apps.auth_mgr.models import ApiCredential

        cache.clear()
        self.pg = ProjectGroup.objects.create(name='主项目', code='tg-proj')
        self.ch = Channel.objects.create(name='官服渠道', code='tg-official')
        self.env = Environment.objects.create(name='单测环境', code='tg-env')
        self.server = Server.objects.create(
            name='local', private_ip='127.0.0.1',
            project_group=self.pg, channel=self.ch, environment=self.env,
        )
        self.service = ServiceType.objects.create(name='基础服务', code='tg-base')
        ServerService.objects.create(server=self.server, service_type=self.service,
                                     status='planned')
        self.task = ExecutionTask.objects.create(
            name='接口触发任务', project_group=self.pg, channel=self.ch, environment=self.env,
        )
        TaskStep.objects.create(task=self.task, order=1, name='模块1',
                                task_type='command', command='echo hi',
                                service_type=self.service)

        raw, key_id, secret_hash = ApiCredential.generate_key()
        self.credential = ApiCredential.objects.create(
            name='tg-ci', key_id=key_id, secret_hash=secret_hash,
            scopes=['task:trigger'], ip_whitelist=self.ALLOWED_IP,
            bound_user=ensure_service_user('tg-ci'),
        )
        self.key = raw
        self.client = APIClient()

    def tearDown(self):
        cache.clear()

    def _trigger(self, key=None, ip=None, body=None):
        headers = {'HTTP_X_API_KEY': key or self.key, 'REMOTE_ADDR': ip or self.ALLOWED_IP}
        return self.client.post(f'/api/exec/tasks/{self.task.id}/trigger/',
                                body or {}, format='json', **headers)

    def test_trigger_creates_execution(self):
        with mock.patch('apps.task_exec.trigger.dispatch', return_value=mock.Mock(id=7)):
            r = self._trigger()
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()['data']['log_id'], 7)
        self.assertEqual(r.json()['data']['server_count'], 1)
        self.task.refresh_from_db()
        self.assertEqual(list(self.task.servers.values_list('id', flat=True)), [self.server.id])

    def test_trigger_requires_scope(self):
        self.credential.scopes = ['artifact:report']
        self.credential.save(update_fields=['scopes'])
        r = self._trigger()
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()['error_code'], 'api_key_scope_denied')

    def test_trigger_respects_ip_whitelist(self):
        r = self._trigger(ip='198.51.100.7')
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()['error_code'], 'api_key_ip_denied')

    def test_trigger_rejected_when_manual_pick_required(self):
        """关掉默认全选的任务本就是要人工确认，接口不能绕过。"""
        ExecutionTask.objects.filter(pk=self.task.pk).update(default_select_all=False)
        r = self._trigger()
        self.assertEqual(r.status_code, 400)
        self.assertIn('不支持接口触发', r.json()['msg'])

    def test_production_requires_explicit_confirmation(self):
        from apps.asset_mgr.models import Environment

        Environment.objects.filter(pk=self.env.pk).update(is_prod=True)
        r = self._trigger()
        self.assertEqual(r.status_code, 400)
        self.assertIn('confirm_production', r.json()['msg'])

        with mock.patch('apps.task_exec.trigger.dispatch', return_value=mock.Mock(id=8)):
            r = self._trigger(body={'confirm_production': True})
        self.assertEqual(r.status_code, 200, r.content)

    def test_trigger_blocked_while_running(self):
        ExecutionTask.objects.filter(pk=self.task.pk).update(status='running')
        r = self._trigger()
        self.assertEqual(r.status_code, 400)
        self.assertIn('正在执行中', r.json()['msg'])

    def test_trigger_records_api_trigger_type(self):
        with mock.patch('apps.task_exec.runner.run_stream', return_value=(0, 'ok')):
            r = self._trigger()
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.task.logs.first().trigger_type, 'api')
        self.assertEqual(self.task.logs.first().triggered_by, self.credential.bound_user)


class SecretVariableTests(TestCase):
    """敏感变量：加密存储、任何读接口不回显、注入 ansible 仍是明文、输出打码。"""

    URL = '/api/exec/variables/'
    SECRET = 'p@ss#word:with-yaml-traps'

    def setUp(self):
        from apps.asset_mgr.models import Channel, Environment, ProjectGroup

        cache.clear()
        self.admin = User.objects.create(username='var_admin', is_staff=True)
        self.admin.set_password('Pass!2345xyz')
        self.admin.save()
        self.member = User.objects.create(username='var_member', is_staff=False)
        self.member.set_password('Pass!2345xyz')
        self.member.save()
        self.pg = ProjectGroup.objects.create(name='主项目', code='sv-proj')
        self.ch = Channel.objects.create(name='官服', code='sv-official')
        self.env = Environment.objects.create(name='测试', code='sv-env')
        self.client = self._client('var_admin')

    def tearDown(self):
        cache.clear()

    def _client(self, username):
        c = APIClient()
        r = c.post('/api/auth/login/', {'username': username, 'password': 'Pass!2345xyz'},
                   format='json')
        c.credentials(HTTP_AUTHORIZATION='Bearer ' + r.json()['access'])
        return c

    def _payload(self, **kwargs):
        data = {'key': 'db_password', 'value': self.SECRET, 'is_secret': True,
                'project_group': self.pg.id, 'channel': self.ch.id, 'environment': self.env.id}
        data.update(kwargs)
        return data

    def _create(self, **kwargs):
        r = self.client.post(self.URL, self._payload(**kwargs), format='json')
        self.assertEqual(r.status_code, 201, r.content)
        return ExecVariable.objects.get(pk=r.json()['id'])

    # ── 存储 ──────────────────────────────────────────────
    def test_stored_encrypted(self):
        var = self._create()
        self.assertTrue(var.is_secret)
        self.assertNotIn(self.SECRET, var.value)   # 库里没有明文
        self.assertTrue(var.value.startswith('gAAAAA'))  # Fernet 密文特征
        self.assertEqual(var.get_value(), self.SECRET)

    def test_secret_requires_value_on_create(self):
        r = self.client.post(self.URL, self._payload(value=''), format='json')
        self.assertEqual(r.status_code, 400)

    def test_plain_variable_still_validates_yaml(self):
        r = self.client.post(self.URL,
                             self._payload(is_secret=False, value='a: [unclosed'),
                             format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('YAML', str(r.json()))

    def test_secret_skips_yaml_validation(self):
        """敏感值按字面串处理，`#`/`:` 不该被当 YAML 语法拒绝或改写。"""
        var = self._create(value='a: [unclosed # trap')
        self.assertEqual(var.get_value(), 'a: [unclosed # trap')

    # ── 不回显 ────────────────────────────────────────────
    def test_value_not_returned_in_list_or_detail(self):
        var = self._create()
        for url in (self.URL, f'{self.URL}{var.id}/'):
            body = self.client.get(url).json()
            text = str(body)
            self.assertNotIn(self.SECRET, text, url)
        detail = self.client.get(f'{self.URL}{var.id}/').json()
        self.assertEqual(detail['value'], '')
        self.assertTrue(detail['has_value'])

    def test_value_not_returned_in_versions(self):
        """当前值藏起来、翻历史就看到了 —— 这条要堵住。"""
        var = self._create()
        body = self.client.get(f'{self.URL}{var.id}/versions/').json()
        self.assertNotIn(self.SECRET, str(body))

    def test_member_cannot_read_secret(self):
        self._create()
        member = self._client('var_member')
        self.assertNotIn(self.SECRET, str(member.get(self.URL).json()))

    # ── reveal ────────────────────────────────────────────
    def test_reveal_requires_admin_and_sudo(self):
        var = self._create()
        member = self._client('var_member')
        self.assertEqual(member.post(f'{self.URL}{var.id}/reveal/').status_code, 403)

        fresh = self._client('var_admin')  # 新会话，未开 sudo 窗口
        r = fresh.post(f'{self.URL}{var.id}/reveal/')
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()['error_code'], 'sudo_required')

        grant_sudo(fresh, 'Pass!2345xyz')
        r = fresh.post(f'{self.URL}{var.id}/reveal/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['data']['value'], self.SECRET)

    # ── 切换标记 ──────────────────────────────────────────
    def test_toggle_plain_to_secret_encrypts_existing_value(self):
        var = self._create(key='plain_key', value='hello', is_secret=False)
        r = self.client.patch(f'{self.URL}{var.id}/', {'is_secret': True}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        var.refresh_from_db()
        self.assertTrue(var.is_secret)
        self.assertNotIn('hello', var.value)
        self.assertEqual(var.get_value(), 'hello')

    def test_toggle_secret_to_plain_decrypts(self):
        var = self._create()
        r = self.client.patch(f'{self.URL}{var.id}/', {'is_secret': False}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        var.refresh_from_db()
        self.assertFalse(var.is_secret)
        self.assertEqual(var.value, self.SECRET)

    def test_rollback_keeps_secret_flag(self):
        var = self._create()
        self.client.patch(f'{self.URL}{var.id}/', {'value': 'newsecret-value'}, format='json')
        r = self.client.post(f'{self.URL}{var.id}/rollback/', {'version': 1}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        var.refresh_from_db()
        self.assertTrue(var.is_secret)
        self.assertEqual(var.get_value(), self.SECRET)

    # ── 注入与对外接口 ────────────────────────────────────
    def test_injected_as_literal_string(self):
        """ansible 侧要拿到明文原样值：这是「加密后我还得能用」的落点。"""
        self._create()
        variables = runner.collect_dim_variables(self.pg.id, self.ch.id, self.env.id)
        self.assertEqual(variables['db_password'], self.SECRET)

    def test_resolved_excludes_secrets_by_default(self):
        self._create()
        r = self.client.get(f'/api/exec/variables/resolved/'
                            f'?project_group={self.pg.code}&channel={self.ch.code}&env={self.env.code}')
        data = r.json()['data']
        self.assertNotIn('db_password', data['vars'])
        self.assertEqual(data['secret_keys_excluded'], ['db_password'])

    def test_resolved_include_secrets_needs_variable_write(self):
        """要真值须有 variable 写档，与 reveal 同一道口径（不是 is_staff）。"""
        from apps.auth_mgr.models import Role

        self._create()
        query = (f'?project_group={self.pg.code}&channel={self.ch.code}'
                 f'&env={self.env.code}&include_secrets=true')
        grant_sudo(self.client)  # 看真值要 sudo 窗口（与 reveal 一致）
        r = self.client.get(f'/api/exec/variables/resolved/{query}')
        self.assertEqual(r.json()['data']['vars']['db_password'], self.SECRET)

        # 只读档（无角色非 staff 回退到「普通运维」矩阵，variable=read）拿不到
        member = self._client('var_member')
        self.assertEqual(member.get(f'/api/exec/variables/resolved/{query}').status_code, 403)

        # 配了 variable 写档的非 staff 角色能拿——以前这里判 is_staff，
        # 同一个人在 reveal 看得到、在这里被拒
        role = Role.objects.create(
            name='变量管理', code='var_writer', permissions={'variable': 'write'})
        writer = User.objects.create(username='var_writer', is_staff=False, role=role)
        writer.set_password('Pass!2345xyz')
        writer.save()
        writer_client = self._client('var_writer')
        grant_sudo(writer_client, 'Pass!2345xyz')
        r = writer_client.get(f'/api/exec/variables/resolved/{query}')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()['data']['vars']['db_password'], self.SECRET)

    def test_resolved_include_secrets_needs_sudo(self):
        """一次拉回该维度下全部密码类变量，暴露面比 reveal 单个还大，同样要 sudo。"""
        self._create()
        query = (f'?project_group={self.pg.code}&channel={self.ch.code}'
                 f'&env={self.env.code}&include_secrets=true')
        fresh = self._client('var_admin')  # 新会话，未开 sudo 窗口
        r = fresh.get(f'/api/exec/variables/resolved/{query}')
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()['error_code'], 'sudo_required')
        # 不带 include_secrets 的普通读取不受影响（脚本日常调的就是这条）
        plain = (f'?project_group={self.pg.code}&channel={self.ch.code}'
                 f'&env={self.env.code}')
        self.assertEqual(fresh.get(f'/api/exec/variables/resolved/{plain}').status_code, 200)

    # ── 输出打码 ──────────────────────────────────────────
    def test_mask_secrets_longest_first(self):
        """短明文是长明文子串时，先换短的会把长的切成拼不回的片段。"""
        text = 'value=abcdef and value=abc'
        masked = live.mask_secrets(text, {'abc', 'abcdef'})
        self.assertNotIn('abcdef', masked)
        self.assertEqual(masked, 'value=*** and value=***')

    def test_live_sink_masks_feed(self):
        sink = live.LiveSink(task_id=1)
        sink.add_secrets({self.SECRET})
        sink.bind([])
        sink.begin(('k', 1), 'header', order=1)
        sink.feed(('k', 1), f'connecting with {self.SECRET}\n')
        self.assertNotIn(self.SECRET, str(sink._live[('k', 1)]['buf']))

    def test_collect_secret_values_skips_short(self):
        self._create(key='short_secret', value='ab')
        self._create(key='long_secret', value='long-enough-secret')
        values = runner.collect_dim_secret_values(self.pg.id, self.ch.id, self.env.id)
        self.assertIn('long-enough-secret', values)
        self.assertNotIn('ab', values)


class CredentialEncryptionTests(TestCase):
    """连接凭据密钥加密存储：库里是密文，ORM 与 inventory 拿到的是明文。"""

    PASSWORD = 'ssh-r00t-pass'
    KEY_PEM = '-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----'

    def test_stored_as_ciphertext(self):
        from django.db import connection

        cred = Credential.objects.create(
            name='enc-cred', username='root', auth_type='password',
            password=self.PASSWORD, become=True, become_password='sudo-pass-1234',
            private_key=self.KEY_PEM,
        )
        with connection.cursor() as cursor:
            cursor.execute(
                'SELECT password, private_key, become_password FROM task_credential WHERE id = %s',
                [cred.id])
            raw = cursor.fetchone()
        for stored in raw:
            self.assertTrue(stored.startswith('gAAAAA'), stored[:30])
        self.assertNotIn(self.PASSWORD, ' '.join(raw))
        self.assertNotIn('BEGIN OPENSSH', ' '.join(raw))

        # ORM 读回明文（读取点零改动的前提）
        fresh = Credential.objects.get(pk=cred.pk)
        self.assertEqual(fresh.password, self.PASSWORD)
        self.assertEqual(fresh.private_key, self.KEY_PEM)
        self.assertEqual(fresh.become_password, 'sudo-pass-1234')

    def test_inventory_gets_plaintext(self):
        """ansible 建连要的是明文——这是「加密后还能用」的落点。"""
        cred = Credential.objects.create(
            name='enc-cred-2', username='root', auth_type='password', password=self.PASSWORD)
        server = Server.objects.create(name='enc-host', private_ip='10.9.9.9')
        text = _inventory_content([server], cred, None, include_secrets=True)
        self.assertIn(self.PASSWORD, text)

    def test_empty_secret_stays_empty(self):
        """空值不加密：序列化器的 has_password 布尔依赖「空即未设置」。"""
        cred = Credential.objects.create(name='enc-cred-3', username='root', auth_type='key')
        self.assertEqual(raw_column('task_credential', 'password', cred.id), '')
        self.assertFalse(Credential.objects.get(pk=cred.pk).password)


def raw_column(table, column, pk):
    """读某行某列的原始库值（绕过字段解密），供加密断言使用。"""
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(f'SELECT {column} FROM {table} WHERE id = %s', [pk])
        return cursor.fetchone()[0]
