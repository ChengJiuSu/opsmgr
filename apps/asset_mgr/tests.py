"""CMDB 资产模块测试：字典种子数据、CRUD 权限、删除保护、服务器外键与筛选。"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from .models import Channel, CloudProvider, Environment, ProjectGroup, Server

User = get_user_model()


class BaseApiTest(TestCase):
    def _client(self, is_staff=True, username=None):
        username = username or ('admin_a' if is_staff else 'user_a')
        u = User.objects.create(username=username, is_staff=is_staff)
        u.set_password('Pass!2345xyz')
        u.save()
        c = APIClient()
        r = c.post('/api/auth/login/', {'username': username, 'password': 'Pass!2345xyz'}, format='json')
        c.credentials(HTTP_AUTHORIZATION='Bearer ' + r.json()['access'])
        return c


class DictSeedTests(TestCase):
    def test_seed_data_created_by_migration(self):
        """迁移应内置常用环境/云厂商/渠道字典。"""
        self.assertEqual(
            set(Environment.objects.values_list('code', flat=True)),
            {'dev', 'test', 'prod'},
        )
        self.assertTrue(Environment.objects.get(code='prod').is_prod)
        self.assertFalse(Environment.objects.get(code='dev').is_prod)
        self.assertIn('aliyun', CloudProvider.objects.values_list('code', flat=True))
        self.assertIn('qixia', Channel.objects.values_list('code', flat=True))


class DictApiTests(BaseApiTest):
    def test_crud_and_permission(self):
        admin = self._client(is_staff=True)
        normal = self._client(is_staff=False)

        # 管理员可创建；普通用户可查、可新增，不可修改/删除
        r = admin.post('/api/assets/project-groups/', {'name': '传奇项目组', 'code': 'legend', 'owner': '张三'}, format='json')
        self.assertEqual(r.status_code, 201)
        gid = r.json()['data']['id'] if 'data' in r.json() else r.json()['id']

        r = normal.post('/api/assets/project-groups/', {'name': '武侠项目组', 'code': 'wuxia'}, format='json')
        self.assertEqual(r.status_code, 201)
        nid = r.json()['data']['id'] if 'data' in r.json() else r.json()['id']

        r = normal.get('/api/assets/project-groups/')
        self.assertEqual(r.status_code, 200)

        r = normal.put(f'/api/assets/project-groups/{nid}/', {'name': '武侠项目组', 'code': 'wuxia', 'owner': '王五'}, format='json')
        self.assertEqual(r.status_code, 403)
        r = normal.patch(f'/api/assets/project-groups/{nid}/', {'enabled': False}, format='json')
        self.assertEqual(r.status_code, 403)
        r = normal.delete(f'/api/assets/project-groups/{nid}/')
        self.assertEqual(r.status_code, 403)

        # 编码唯一性
        r = admin.post('/api/assets/project-groups/', {'name': '重复', 'code': 'legend'}, format='json')
        self.assertEqual(r.status_code, 400)

        # 更新与删除
        r = admin.put(f'/api/assets/project-groups/{gid}/', {'name': '传奇项目组', 'code': 'legend', 'owner': '李四'}, format='json')
        self.assertEqual(r.status_code, 200)
        r = admin.delete(f'/api/assets/project-groups/{gid}/')
        self.assertEqual(r.status_code, 204)

    def test_enabled_and_keyword_filter(self):
        admin = self._client()
        Environment.objects.filter(code='dev').update(enabled=False)
        r = admin.get('/api/assets/environments/', {'enabled': 'true'})
        codes = [item['code'] for item in r.json()['data']['results']]
        self.assertNotIn('dev', codes)
        r = admin.get('/api/assets/environments/', {'keyword': '生产'})
        self.assertEqual([i['code'] for i in r.json()['data']['results']], ['prod'])


class ProtectedDeleteTests(BaseApiTest):
    def test_referenced_dict_cannot_be_deleted(self):
        admin = self._client()
        env = Environment.objects.get(code='prod')
        Server.objects.create(name='web1', ip='10.0.0.1', environment=env)

        r = admin.delete(f'/api/assets/environments/{env.id}/')
        self.assertEqual(r.status_code, 400)
        self.assertIn('仍被 1 台服务器引用', r.json()['msg'])
        self.assertTrue(Environment.objects.filter(id=env.id).exists())

        # 解除引用后可删除
        Server.objects.all().delete()
        r = admin.delete(f'/api/assets/environments/{env.id}/')
        self.assertEqual(r.status_code, 204)


class ServerApiTests(BaseApiTest):
    def setUp(self):
        self.admin = self._client()
        self.envProd = Environment.objects.get(code='prod')
        self.envTest = Environment.objects.get(code='test')
        self.group = ProjectGroup.objects.create(name='仙侠项目组', code='xianxia')
        self.channel = Channel.objects.get(code='qixia')
        self.provider = CloudProvider.objects.get(code='aliyun')

    def test_create_with_fk_and_display_fields(self):
        r = self.admin.post('/api/assets/servers/', {
            'name': 'game-01', 'ip': '10.1.0.1',
            'environment': self.envProd.id, 'project_group': self.group.id,
            'channel': self.channel.id, 'cloud_provider': self.provider.id,
            'region': 'cn-hangzhou',
        }, format='json')
        self.assertEqual(r.status_code, 201)
        data = r.json()['data'] if 'data' in r.json() else r.json()
        self.assertEqual(data['environment_name'], '生产')
        self.assertTrue(data['environment_is_prod'])
        self.assertEqual(data['project_group_name'], '仙侠项目组')
        self.assertEqual(data['cloud_provider_name'], '阿里云')

        # 外键全部留空也允许创建
        r = self.admin.post('/api/assets/servers/', {'name': 'tmp-01', 'ip': '10.1.0.9'}, format='json')
        self.assertEqual(r.status_code, 201)

    def test_normal_user_can_create_but_not_modify(self):
        normal = self._client(is_staff=False)
        r = normal.post('/api/assets/servers/', {'name': 'user-01', 'ip': '10.2.0.1'}, format='json')
        self.assertEqual(r.status_code, 201)
        sid = r.json()['data']['id'] if 'data' in r.json() else r.json()['id']

        r = normal.put(f'/api/assets/servers/{sid}/', {'name': 'user-01x', 'ip': '10.2.0.1'}, format='json')
        self.assertEqual(r.status_code, 403)
        r = normal.delete(f'/api/assets/servers/{sid}/')
        self.assertEqual(r.status_code, 403)

    def test_filter_by_code_and_id(self):
        Server.objects.create(name='p1', ip='10.1.0.2', environment=self.envProd, project_group=self.group)
        Server.objects.create(name='t1', ip='10.1.0.3', environment=self.envTest)

        r = self.admin.get('/api/assets/servers/', {'env': 'prod'})
        self.assertEqual([i['name'] for i in r.json()['data']['results']], ['p1'])

        r = self.admin.get('/api/assets/servers/', {'env': str(self.envTest.id)})
        self.assertEqual([i['name'] for i in r.json()['data']['results']], ['t1'])

        r = self.admin.get('/api/assets/servers/', {'project_group': 'xianxia'})
        self.assertEqual([i['name'] for i in r.json()['data']['results']], ['p1'])

    def test_stats_by_env_and_project(self):
        Server.objects.create(name='p1', ip='10.1.0.2', environment=self.envProd, project_group=self.group)
        Server.objects.create(name='p2', ip='10.1.0.4', environment=self.envProd)
        r = self.admin.get('/api/assets/servers/stats/')
        data = r.json()['data']
        self.assertEqual(data['by_env'].get('生产'), 2)
        self.assertEqual(data['by_project'].get('仙侠项目组'), 1)
        self.assertEqual(data['by_project'].get('未分配'), 1)
