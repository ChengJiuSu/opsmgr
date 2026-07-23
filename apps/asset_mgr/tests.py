"""CMDB 资产模块测试：字典种子数据、CRUD 权限、删除保护、服务器外键与筛选。"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from .models import Channel, CloudProvider, Environment, ProjectGroup, Server, ServerService, ServiceType

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

    def test_create_with_service_types_generates_process_name(self):
        """新增主机时可选分配服务：直接落 planned 记录并自动生成进程名。"""
        from .models import ServerService, ServiceType

        nginx = ServiceType.objects.create(name='Nginx', code='nginx')
        payload = {
            'name': 'idc-cn-cd-develop-nginx-01', 'ip': '10.1.1.1',
            'environment': self.envProd.id, 'project_group': self.group.id,
            'channel': self.channel.id, 'cloud_provider': self.provider.id,
            'service_type_ids': [nginx.id],
        }
        def _services(resp):
            body = resp.json()
            return (body['data'] if 'data' in body else body)['services']

        r = self.admin.post('/api/assets/servers/', payload, format='json')
        self.assertEqual(r.status_code, 201)
        services = _services(r)
        self.assertEqual(services[0]['status'], 'planned')
        self.assertEqual(services[0]['process_name'], 'nginx-01')
        self.assertEqual(services[0]['process_count'], 1)

        # 相同云厂商+项目组+环境+渠道的第二台 nginx → nginx-02（与主机名序号无关）
        r = self.admin.post('/api/assets/servers/', {
            **payload, 'name': 'idc-cn-cd-develop-web-09', 'ip': '10.1.1.2',
        }, format='json')
        self.assertEqual(_services(r)[0]['process_name'], 'nginx-02')

        # 分组维度不同（换云厂商）→ 序号独立，从 01 重新开始
        other = CloudProvider.objects.get(code='tencent')
        r = self.admin.post('/api/assets/servers/', {
            **payload, 'name': 'tx-nginx', 'ip': '10.1.1.3', 'cloud_provider': other.id,
        }, format='json')
        self.assertEqual(_services(r)[0]['process_name'], 'nginx-01')

        # 停用的服务类型拒绝分配
        disabled = ServiceType.objects.create(name='停用', code='disabled-x', enabled=False)
        r = self.admin.post('/api/assets/servers/', {
            **payload, 'name': 'bad', 'ip': '10.1.1.4', 'service_type_ids': [disabled.id],
        }, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertEqual(ServerService.objects.count(), 3)

    def test_server_list_assigned_filter(self):
        """assigned=service_type 仅返回有服务记录的主机（服务器分配页数据源）。"""
        from .models import ServerService, ServiceType

        nginx = ServiceType.objects.create(name='Nginx', code='nginx')
        planned = Server.objects.create(name='planned-01', ip='10.1.2.1')
        Server.objects.create(name='raw-01', ip='10.1.2.2')
        ServerService.objects.create(server=planned, service_type=nginx, status='planned')

        r = self.admin.get('/api/assets/servers/', {'assigned': 'service_type'})
        self.assertEqual([i['name'] for i in r.json()['data']['results']], ['planned-01'])

    def test_server_list_executable_filter(self):
        """executable=true 仅返回已规划/已部署服务的主机（任务表单数据源），offline 不算。"""
        from .models import ServerService, ServiceType

        nginx = ServiceType.objects.create(name='Nginx', code='nginx')
        planned = Server.objects.create(name='planned-01', ip='10.1.3.1')
        deployed = Server.objects.create(name='deployed-01', ip='10.1.3.2')
        offline = Server.objects.create(name='offline-01', ip='10.1.3.3')
        Server.objects.create(name='raw-01', ip='10.1.3.4')
        ServerService.objects.create(server=planned, service_type=nginx, status='planned')
        ServerService.objects.create(server=deployed, service_type=nginx, status='deployed')
        link = ServerService.objects.create(server=offline, service_type=nginx, status='planned')
        ServerService.objects.filter(pk=link.pk).update(status='offline')

        r = self.admin.get('/api/assets/servers/', {'executable': 'true'})
        names = sorted(i['name'] for i in r.json()['data']['results'])
        self.assertEqual(names, ['deployed-01', 'planned-01'])

    def test_page_size_zero_returns_all(self):
        """page_size=0 跳过分页一次性返回全量，响应结构与分页时一致。"""
        for i in range(25):  # 超过默认页大小 20
            Server.objects.create(name=f'bulk-{i:02d}', ip=f'10.9.0.{i + 1}')

        r = self.admin.get('/api/assets/servers/', {'page_size': 0})
        data = r.json()['data']
        self.assertEqual(data['total'], 25)
        self.assertEqual(len(data['results']), 25)
        self.assertEqual(data['page'], 1)

        # 常规分页行为不受影响
        r = self.admin.get('/api/assets/servers/', {'page_size': 10, 'page': 2})
        data = r.json()['data']
        self.assertEqual(data['total'], 25)
        self.assertEqual(len(data['results']), 10)
        self.assertEqual(data['page'], 2)

    def test_stats_by_env_and_project(self):
        Server.objects.create(name='p1', ip='10.1.0.2', environment=self.envProd, project_group=self.group)
        Server.objects.create(name='p2', ip='10.1.0.4', environment=self.envProd)
        r = self.admin.get('/api/assets/servers/stats/')
        data = r.json()['data']
        self.assertEqual(data['by_env'].get('生产'), 2)
        self.assertEqual(data['by_project'].get('仙侠项目组'), 1)
        self.assertEqual(data['by_project'].get('未分配'), 1)


class ServerBatchApiTests(BaseApiTest):
    """批量编辑/删除接口：字段白名单、部署保护、权限。"""

    def setUp(self):
        self.admin = self._client()
        self.s1 = Server.objects.create(name='b-01', ip='10.9.0.1')
        self.s2 = Server.objects.create(name='b-02', ip='10.9.0.2')

    def test_batch_update_fields(self):
        r = self.admin.post('/api/assets/servers/batch/update/', {
            'server_ids': [self.s1.id, self.s2.id],
            'update': {'status': 'maintenance', 'ssh_port': 2222, 'region': 'cn-sh'},
        }, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['data']['updated'], 2)
        self.s1.refresh_from_db()
        self.assertEqual((self.s1.status, self.s1.ssh_port, self.s1.region), ('maintenance', 2222, 'cn-sh'))

    def test_batch_update_rejects_unknown_field_and_bad_values(self):
        r = self.admin.post('/api/assets/servers/batch/update/', {
            'server_ids': [self.s1.id], 'update': {'ip': '1.1.1.1'},
        }, format='json')
        self.assertEqual(r.status_code, 400)
        r = self.admin.post('/api/assets/servers/batch/update/', {
            'server_ids': [self.s1.id], 'update': {'ssh_port': 70000},
        }, format='json')
        self.assertEqual(r.status_code, 400)
        r = self.admin.post('/api/assets/servers/batch/update/', {
            'server_ids': [self.s1.id], 'update': {'status': 'bad'},
        }, format='json')
        self.assertEqual(r.status_code, 400)

    def test_batch_delete_blocks_deployed(self):
        st = ServiceType.objects.create(name='网关', code='gate')
        ServerService.objects.create(server=self.s1, service_type=st, status='deployed')
        r = self.admin.post('/api/assets/servers/batch/delete/', {
            'server_ids': [self.s1.id, self.s2.id],
        }, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('已部署', r.json()['msg'])
        # 无部署服务的主机可删
        r = self.admin.post('/api/assets/servers/batch/delete/', {
            'server_ids': [self.s2.id],
        }, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Server.objects.filter(id=self.s2.id).exists())

    def test_batch_requires_admin(self):
        normal = self._client(is_staff=False)
        for url in ('/api/assets/servers/batch/update/', '/api/assets/servers/batch/delete/',
                    '/api/assets/servers/register-jumpserver/'):
            r = normal.post(url, {'server_ids': [self.s1.id], 'update': {'status': 'running'}}, format='json')
            self.assertEqual(r.status_code, 403, url)


class ServerImportExportTests(BaseApiTest):
    """CSV 模板 / 导入 / 导出。"""

    def setUp(self):
        self.admin = self._client()
        self.group = ProjectGroup.objects.create(name='传奇项目组', code='legend')

    def _import(self, content, mode='skip'):
        from django.core.files.uploadedfile import SimpleUploadedFile
        f = SimpleUploadedFile('servers.csv', content.encode('utf-8'), content_type='text/csv')
        return self.admin.post(f'/api/assets/servers/import/?mode={mode}',
                               {'file': f, 'mode': mode}, format='multipart')

    def test_template_download(self):
        r = self.admin.get('/api/assets/servers/import-template/')
        self.assertEqual(r.status_code, 200)
        self.assertIn('text/csv', r['Content-Type'])
        body = r.content.decode('utf-8-sig')
        self.assertTrue(body.startswith('name,ip,'))

    def test_import_create_and_skip_update(self):
        csv_text = (
            'name,ip,os,cpu,memory,status,project_group,channel,environment,cloud_provider,region,ssh_port,ssh_user,remark\n'
            'imp-01,10.8.0.1,Ubuntu,4,8192,running,legend,,prod,aliyun,cn-hz,22,root,首次导入\n'
            'imp-02,10.8.0.2,,,,,,,,,,,,\n'
        )
        r = self._import(csv_text)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()['data'], {'created': 2, 'updated': 0, 'skipped': 0})
        srv = Server.objects.get(ip='10.8.0.1')
        self.assertEqual(srv.project_group.code, 'legend')
        self.assertEqual(srv.environment.code, 'prod')
        self.assertEqual(Server.objects.get(ip='10.8.0.2').ssh_user, 'root')

        # skip 模式重复导入全部跳过
        r = self._import(csv_text)
        self.assertEqual(r.json()['data'], {'created': 0, 'updated': 0, 'skipped': 2})

        # update 模式按 IP 更新
        r = self._import(
            'name,ip,status\nimp-01-renamed,10.8.0.1,maintenance\n', mode='update')
        self.assertEqual(r.json()['data'], {'created': 0, 'updated': 1, 'skipped': 0})
        srv.refresh_from_db()
        self.assertEqual((srv.name, srv.status), ('imp-01-renamed', 'maintenance'))

    def test_import_validation_errors_abort_all(self):
        csv_text = (
            'name,ip,status,project_group\n'
            'ok-01,10.8.1.1,running,legend\n'
            ',bad-ip,flying,nope\n'
        )
        r = self._import(csv_text)
        self.assertEqual(r.status_code, 400)
        errors = r.json()['data']['errors']
        self.assertEqual(errors[0]['line'], 3)
        joined = ' '.join(errors[0]['errors'])
        self.assertIn('name 不能为空', joined)
        self.assertIn('ip 格式非法', joined)
        # 任一行有错则整体不写库
        self.assertFalse(Server.objects.filter(ip='10.8.1.1').exists())

    def test_export_roundtrip(self):
        Server.objects.create(name='exp-01', ip='10.8.2.1', project_group=self.group, ssh_port=2222)
        r = self.admin.get('/api/assets/servers/export/')
        self.assertEqual(r.status_code, 200)
        body = r.content.decode('utf-8-sig')
        self.assertIn('exp-01,10.8.2.1', body)
        self.assertIn('legend', body)


class ServerJumpServerTests(BaseApiTest):
    """JumpServer 注册接口：配置缺失报错、逐台结果汇总（mock HTTP）。"""

    def setUp(self):
        self.admin = self._client()
        self.s1 = Server.objects.create(name='js-01', ip='10.7.0.1')

    def test_missing_config_returns_friendly_error(self):
        r = self.admin.post('/api/assets/servers/register-jumpserver/', {
            'server_ids': [self.s1.id],
        }, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('jumpserver.url', r.json()['msg'])

    def test_register_with_mocked_api(self):
        from unittest import mock
        from apps.system_cfg.models import SystemConfig
        SystemConfig.objects.create(key='jumpserver.url', value='http://js.local')
        SystemConfig.objects.create(key='jumpserver.token', value='tkn')

        def fake_response(status=200, payload=None):
            resp = mock.Mock()
            resp.status_code = status
            resp.ok = 200 <= status < 300
            resp.json.return_value = payload if payload is not None else {}
            resp.text = ''
            return resp

        with mock.patch('common.jumpserver.requests.Session') as session_cls:
            session = session_cls.return_value
            session.headers = {}
            # 查重返回空 → 创建成功
            session.get.return_value = fake_response(200, {'results': []})
            session.post.return_value = fake_response(201, {'id': 'x'})
            r = self.admin.post('/api/assets/servers/register-jumpserver/', {
                'server_ids': [self.s1.id],
            }, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        data = r.json()['data']
        self.assertEqual((data['total'], data['succeeded'], data['failed']), (1, 1, 0))
        self.assertEqual(data['results'][0]['msg'], '注册成功')

    def test_duplicate_names_in_selection_rejected(self):
        from apps.system_cfg.models import SystemConfig
        SystemConfig.objects.create(key='jumpserver.url', value='http://js.local')
        SystemConfig.objects.create(key='jumpserver.token', value='tkn')
        s2 = Server.objects.create(name='js-01', ip='10.7.0.2')
        r = self.admin.post('/api/assets/servers/register-jumpserver/', {
            'server_ids': [self.s1.id, s2.id],
        }, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('重名', r.json()['msg'])
        self.assertIn('js-01', r.json()['msg'])

    def test_name_conflict_on_jumpserver_rejected(self):
        from unittest import mock
        from apps.system_cfg.models import SystemConfig
        SystemConfig.objects.create(key='jumpserver.url', value='http://js.local')
        SystemConfig.objects.create(key='jumpserver.token', value='tkn')

        def fake_response(status=200, payload=None, text=''):
            resp = mock.Mock()
            resp.status_code = status
            resp.ok = 200 <= status < 300
            resp.json.return_value = payload if payload is not None else {}
            resp.text = text
            return resp

        with mock.patch('common.jumpserver.requests.Session') as session_cls:
            session = session_cls.return_value
            session.headers = {}
            # IP 查重为空，但创建时名称与 JumpServer 已有资产冲突
            session.get.return_value = fake_response(200, {'results': []})
            session.post.return_value = fake_response(
                400, text='{"name":["This field must be unique."]}')
            r = self.admin.post('/api/assets/servers/register-jumpserver/', {
                'server_ids': [self.s1.id],
            }, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('重名', r.json()['msg'])
