"""配置管理模块测试：批量分配、字段清空、校验与权限、分配概览、服务类型规划。"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.asset_mgr.models import Channel, Environment, ProjectGroup, Server, ServerService, ServiceType

User = get_user_model()

ASSIGN_URL = '/api/config/allocation/assign/'
OVERVIEW_URL = '/api/config/allocation/overview/'
PLAN_URL = '/api/config/services/plan/'
MARK_URL = '/api/config/services/mark/'
UNPLAN_URL = '/api/config/services/unplan/'
PROCESS_URL = '/api/config/services/process/'


class BaseApiTest(TestCase):
    def _client(self, is_staff=True, username=None):
        username = username or ('admin_c' if is_staff else 'user_c')
        u = User.objects.create(username=username, is_staff=is_staff)
        u.set_password('Pass!2345xyz')
        u.save()
        c = APIClient()
        r = c.post('/api/auth/login/', {'username': username, 'password': 'Pass!2345xyz'}, format='json')
        c.credentials(HTTP_AUTHORIZATION='Bearer ' + r.json()['access'])
        return c

    def setUp(self):
        self.admin = self._client()
        self.envProd = Environment.objects.get(code='prod')
        self.group = ProjectGroup.objects.create(name='仙侠项目组', code='xianxia')
        self.channel = Channel.objects.get(code='qixia')
        self.s1 = Server.objects.create(name='s1', ip='10.2.0.1')
        self.s2 = Server.objects.create(name='s2', ip='10.2.0.2', region='cn-shanghai')


class BatchAssignTests(BaseApiTest):
    def test_assign_multiple_fields(self):
        r = self.admin.post(ASSIGN_URL, {
            'server_ids': [self.s1.id, self.s2.id],
            'assign': {'project_group': self.group.id, 'environment': self.envProd.id},
        }, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['data']['updated'], 2)
        self.s1.refresh_from_db()
        self.s2.refresh_from_db()
        self.assertEqual(self.s1.project_group_id, self.group.id)
        self.assertEqual(self.s2.environment_id, self.envProd.id)
        # 未提供的字段保持不动
        self.assertEqual(self.s2.region, 'cn-shanghai')

    def test_null_clears_and_region_text(self):
        self.s1.environment = self.envProd
        self.s1.save()
        r = self.admin.post(ASSIGN_URL, {
            'server_ids': [self.s1.id],
            'assign': {'environment': None, 'region': 'cn-hangzhou'},
        }, format='json')
        self.assertEqual(r.status_code, 200)
        self.s1.refresh_from_db()
        self.assertIsNone(self.s1.environment_id)
        self.assertEqual(self.s1.region, 'cn-hangzhou')

    def test_validations(self):
        # 空参数
        r = self.admin.post(ASSIGN_URL, {'server_ids': [], 'assign': {'region': 'x'}}, format='json')
        self.assertEqual(r.status_code, 400)
        r = self.admin.post(ASSIGN_URL, {'server_ids': [self.s1.id], 'assign': {}}, format='json')
        self.assertEqual(r.status_code, 400)
        # 未知字段
        r = self.admin.post(ASSIGN_URL, {'server_ids': [self.s1.id], 'assign': {'foo': 1}}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('不支持的分配字段', r.json()['msg'])
        # 无效服务器 id
        r = self.admin.post(ASSIGN_URL, {'server_ids': [self.s1.id, 99999], 'assign': {'region': 'x'}}, format='json')
        self.assertEqual(r.status_code, 400)
        # 停用的字典项拒绝分配
        disabled = ProjectGroup.objects.create(name='停用组', code='disabled-group', enabled=False)
        r = self.admin.post(ASSIGN_URL, {
            'server_ids': [self.s1.id], 'assign': {'project_group': disabled.id},
        }, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('项目组不存在或已停用', r.json()['msg'])

    def test_permission_admin_only(self):
        normal = self._client(is_staff=False)
        r = normal.post(ASSIGN_URL, {
            'server_ids': [self.s1.id], 'assign': {'project_group': self.group.id},
        }, format='json')
        self.assertEqual(r.status_code, 403)
        # 普通用户可查看概览
        r = normal.get(OVERVIEW_URL)
        self.assertEqual(r.status_code, 200)


class OverviewTests(BaseApiTest):
    def test_overview_counts(self):
        self.s1.project_group = self.group
        self.s1.environment = self.envProd
        self.s1.save()
        r = self.admin.get(OVERVIEW_URL)
        data = r.json()['data']
        self.assertEqual(data['total'], 2)
        self.assertEqual(data['unassigned']['project_group'], 1)
        self.assertEqual(data['unassigned']['channel'], 2)
        self.assertEqual(data['by_project'].get('仙侠项目组'), 1)
        self.assertEqual(data['by_env'].get('未分配'), 1)


class ServicePlanTests(BaseApiTest):
    """服务规划：批量创建 planned 记录、幂等、入参校验、状态流转与取消规划。"""

    def setUp(self):
        super().setUp()
        self.game = ServiceType.objects.create(name='游戏服', code='game-server')
        self.nginx = ServiceType.objects.create(name='Nginx', code='nginx')

    def _plan(self, server_ids, type_ids):
        return self.admin.post(PLAN_URL, {
            'server_ids': server_ids, 'service_type_ids': type_ids,
        }, format='json')

    def test_plan_and_idempotent(self):
        r = self._plan([self.s1.id, self.s2.id], [self.game.id, self.nginx.id])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['data']['created'], 4)
        self.assertEqual(ServerService.objects.filter(status='planned').count(), 4)
        # 重复规划：全部跳过，不报错
        r = self._plan([self.s1.id, self.s2.id], [self.game.id, self.nginx.id])
        self.assertEqual(r.json()['data']['created'], 0)
        self.assertEqual(r.json()['data']['skipped'], 4)

    def test_plan_generates_process_name_and_count(self):
        """规划时自动生成进程名（同分组维度按服务类型递增），process_count 应用到全部记录。"""
        r = self.admin.post(PLAN_URL, {
            'server_ids': [self.s1.id, self.s2.id],
            'service_type_ids': [self.nginx.id],
            'process_count': 4,
        }, format='json')
        self.assertEqual(r.status_code, 200)
        # s1/s2 归属维度相同（均未分配）→ 同组编号 nginx-01 / nginx-02
        names = set(ServerService.objects.values_list('process_name', flat=True))
        self.assertEqual(names, {'nginx-01', 'nginx-02'})
        self.assertEqual(
            set(ServerService.objects.values_list('process_count', flat=True)), {4},
        )

        # 取消规划空出的序号可复用：删除 nginx-01 后新规划的主机补位 01
        first = ServerService.objects.get(process_name='nginx-01')
        self.admin.post(UNPLAN_URL, {
            'server_ids': [first.server_id], 'service_type_id': self.nginx.id,
        }, format='json')
        s3 = Server.objects.create(name='s3', ip='10.2.0.3')
        self._plan([s3.id], [self.nginx.id])
        self.assertEqual(
            ServerService.objects.get(server=s3).process_name, 'nginx-01',
        )

    def test_plan_process_name_scoped_by_dims(self):
        """归属维度不同的主机序号互相独立。"""
        self.s1.project_group = self.group
        self.s1.save()
        self._plan([self.s1.id, self.s2.id], [self.nginx.id])
        # s1（仙侠项目组）与 s2（未分配）不同组，各自都是 nginx-01
        self.assertEqual(
            list(ServerService.objects.values_list('process_name', flat=True)),
            ['nginx-01', 'nginx-01'],
        )

    def test_process_count_update_increase_only(self):
        """进程数修改：可调大；调小整批拒绝；未规划的主机报错。"""
        self._plan([self.s1.id], [self.nginx.id])

        # 调大 1 → 3
        r = self.admin.post(PROCESS_URL, {
            'server_ids': [self.s1.id], 'service_type_id': self.nginx.id, 'process_count': 3,
        }, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['data']['updated'], 1)
        self.assertEqual(ServerService.objects.get(server=self.s1).process_count, 3)

        # 调小 3 → 2 拒绝
        r = self.admin.post(PROCESS_URL, {
            'server_ids': [self.s1.id], 'service_type_id': self.nginx.id, 'process_count': 2,
        }, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('仅允许调大', r.json()['msg'])
        self.assertEqual(ServerService.objects.get(server=self.s1).process_count, 3)

        # 等值提交视为无操作
        r = self.admin.post(PROCESS_URL, {
            'server_ids': [self.s1.id], 'service_type_id': self.nginx.id, 'process_count': 3,
        }, format='json')
        self.assertEqual(r.json()['data']['updated'], 0)

        # 未规划的主机报错；非法 process_count 报错
        r = self.admin.post(PROCESS_URL, {
            'server_ids': [self.s2.id], 'service_type_id': self.nginx.id, 'process_count': 2,
        }, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('尚未规划', r.json()['msg'])
        r = self.admin.post(PROCESS_URL, {
            'server_ids': [self.s1.id], 'service_type_id': self.nginx.id, 'process_count': 0,
        }, format='json')
        self.assertEqual(r.status_code, 400)

        # 权限：仅管理员
        normal = self._client(is_staff=False, username='user_p')
        r = normal.post(PROCESS_URL, {
            'server_ids': [self.s1.id], 'service_type_id': self.nginx.id, 'process_count': 5,
        }, format='json')
        self.assertEqual(r.status_code, 403)

    def test_plan_validations(self):
        r = self._plan([], [self.game.id])
        self.assertEqual(r.status_code, 400)
        r = self._plan([self.s1.id], [])
        self.assertEqual(r.status_code, 400)
        r = self._plan([self.s1.id, 99999], [self.game.id])
        self.assertEqual(r.status_code, 400)
        # process_count 必须为正整数
        r = self.admin.post(PLAN_URL, {
            'server_ids': [self.s1.id], 'service_type_ids': [self.game.id], 'process_count': 0,
        }, format='json')
        self.assertEqual(r.status_code, 400)
        r = self.admin.post(PLAN_URL, {
            'server_ids': [self.s1.id], 'service_type_ids': [self.game.id], 'process_count': 'x',
        }, format='json')
        self.assertEqual(r.status_code, 400)
        # 停用的服务类型拒绝规划
        disabled = ServiceType.objects.create(name='停用服务', code='disabled-svc', enabled=False)
        r = self._plan([self.s1.id], [disabled.id])
        self.assertEqual(r.status_code, 400)
        self.assertIn('无效或已停用', r.json()['msg'])

    def test_mark_transitions(self):
        self._plan([self.s1.id], [self.game.id])
        # planned → deployed
        r = self.admin.post(MARK_URL, {
            'server_ids': [self.s1.id], 'service_type_id': self.game.id, 'status': 'deployed',
        }, format='json')
        self.assertEqual(r.status_code, 200)
        link = ServerService.objects.get(server=self.s1, service_type=self.game)
        self.assertEqual(link.status, 'deployed')
        self.assertIsNotNone(link.deployed_at)
        # planned → offline 不允许（未部署无从下线）：先回退再验证
        self.admin.post(MARK_URL, {
            'server_ids': [self.s1.id], 'service_type_id': self.game.id, 'status': 'planned',
        }, format='json')
        r = self.admin.post(MARK_URL, {
            'server_ids': [self.s1.id], 'service_type_id': self.game.id, 'status': 'offline',
        }, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('不允许流转', r.json()['msg'])
        # 未规划的主机直接流转报错
        r = self.admin.post(MARK_URL, {
            'server_ids': [self.s2.id], 'service_type_id': self.game.id, 'status': 'deployed',
        }, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('尚未规划', r.json()['msg'])

    def test_unplan_only_planned(self):
        self._plan([self.s1.id, self.s2.id], [self.game.id])
        self.admin.post(MARK_URL, {
            'server_ids': [self.s1.id], 'service_type_id': self.game.id, 'status': 'deployed',
        }, format='json')
        # s1 已部署，整批取消被拒绝
        r = self.admin.post(UNPLAN_URL, {
            'server_ids': [self.s1.id, self.s2.id], 'service_type_id': self.game.id,
        }, format='json')
        self.assertEqual(r.status_code, 400)
        # 仅 planned 的 s2 可取消
        r = self.admin.post(UNPLAN_URL, {
            'server_ids': [self.s2.id], 'service_type_id': self.game.id,
        }, format='json')
        self.assertEqual(r.json()['data']['deleted'], 1)

    def test_service_type_delete_protected(self):
        """服务类型仍被主机服务记录引用时禁止删除。"""
        self._plan([self.s1.id], [self.game.id])
        r = self.admin.delete(f'/api/assets/service-types/{self.game.id}/')
        self.assertEqual(r.status_code, 400)
        self.assertIn('无法删除', r.json()['msg'])

    def test_overview_and_server_filters(self):
        self._plan([self.s1.id], [self.game.id])
        r = self.admin.get(OVERVIEW_URL)
        data = r.json()['data']
        self.assertEqual(data['unassigned']['service_type'], 1)
        self.assertEqual(data['by_service']['游戏服']['planned'], 1)
        # 服务器列表按服务类型/状态筛选
        r = self.admin.get('/api/assets/servers/', {'service_type': 'game-server'})
        self.assertEqual([i['name'] for i in r.json()['data']['results']], ['s1'])
        r = self.admin.get('/api/assets/servers/', {'service_status': 'deployed'})
        self.assertEqual(r.json()['data']['results'], [])
        # 未规划服务筛选
        r = self.admin.get('/api/assets/servers/', {'unassigned': 'service_type'})
        self.assertEqual([i['name'] for i in r.json()['data']['results']], ['s2'])
        # 序列化器带出 services 摘要
        r = self.admin.get('/api/assets/servers/', {'service_type': 'game-server'})
        services = r.json()['data']['results'][0]['services']
        self.assertEqual(services[0]['service_type_code'], 'game-server')
        self.assertEqual(services[0]['status'], 'planned')

    def test_permission_admin_only(self):
        normal = self._client(is_staff=False, username='user_svc')
        r = normal.post(PLAN_URL, {
            'server_ids': [self.s1.id], 'service_type_ids': [self.game.id],
        }, format='json')
        self.assertEqual(r.status_code, 403)


class UnassignedFilterTests(BaseApiTest):
    def test_server_list_unassigned_param(self):
        self.s1.project_group = self.group
        self.s1.environment = self.envProd
        self.s1.channel = self.channel
        self.s1.save()
        # 指定维度未分配
        r = self.admin.get('/api/assets/servers/', {'unassigned': 'project_group'})
        self.assertEqual([i['name'] for i in r.json()['data']['results']], ['s2'])
        # 任一维度未分配（s1 云厂商仍为空，也应命中）
        r = self.admin.get('/api/assets/servers/', {'unassigned': 'any'})
        names = {i['name'] for i in r.json()['data']['results']}
        self.assertEqual(names, {'s1', 's2'})
