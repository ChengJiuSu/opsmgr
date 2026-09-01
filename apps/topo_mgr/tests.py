"""拓扑画布模块测试：模型约束、画布 CRUD 与权限、整图读写、自动编排、候选与元信息。"""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db.utils import IntegrityError
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.asset_mgr.models import (
    Channel,
    CloudProvider,
    Environment,
    ProjectGroup,
    Server,
)
from apps.cloud_mgr.models import CertDeployment, Certificate, CloudAccount, DnsRecord, Domain

from .models import TopoCanvas, TopoEdge, TopoNode

User = get_user_model()


class BaseTopoTest(TestCase):
    def setUp(self):
        self.group = ProjectGroup.objects.create(name='传奇项目组', code='p1105')
        self.channel = Channel.objects.create(name='星联', code='starunion')
        self.env = Environment.objects.get(code='prod')
        self.provider = CloudProvider.objects.get(code='aliyun')
        self.canvas = TopoCanvas.objects.create(
            name='加速链路', slug='ingress-path', project_group=self.group,
            channel=self.channel, environment=self.env, region='us',
        )

    def _client(self, is_staff=True, username=None):
        username = username or ('admin_t' if is_staff else 'user_t')
        u = User.objects.create(username=username, is_staff=is_staff)
        u.set_password('Pass!2345xyz')
        u.save()
        c = APIClient()
        r = c.post('/api/auth/login/',
                   {'username': username, 'password': 'Pass!2345xyz'}, format='json')
        c.credentials(HTTP_AUTHORIZATION='Bearer ' + r.json()['access'])
        return c

    def _server(self, name='aws-usw-p1105-01', region='us', **kwargs):
        return Server.objects.create(
            name=name, private_ip=kwargs.pop('private_ip', '10.0.0.1'),
            project_group=self.group, channel=self.channel, environment=self.env,
            cloud_provider=self.provider, region=region, **kwargs)

    def _domain_record(self, server, rr='game'):
        account = CloudAccount.objects.create(
            name=f'账号-{rr}', provider=self.provider, access_key='AK')
        domain = Domain.objects.create(name=f'{rr}.example.com', cloud_account=account)
        record = DnsRecord.objects.create(
            domain=domain, rr='@', type='A', value=server.conn_ip,
            target_type='server', server=server)
        return domain, record


class ModelConstraintTests(BaseTopoTest):
    def test_external_node_rejects_ref_id(self):
        node = TopoNode(canvas=self.canvas, source='external', kind='aws_ga',
                        label='p1105-us-ga', ref_id=1)
        with self.assertRaises(ValidationError):
            node.full_clean(exclude=['canvas'])

    def test_ref_node_rejects_external_kind(self):
        node = TopoNode(canvas=self.canvas, source='ref', kind='aws_ga',
                        label='x', ref_id=1)
        with self.assertRaises(ValidationError):
            node.full_clean(exclude=['canvas'])

    def test_same_entity_twice_on_one_canvas_is_rejected(self):
        server = self._server()
        TopoNode.objects.create(canvas=self.canvas, source='ref', kind='server',
                                ref_id=server.id, label=server.name)
        with self.assertRaises(IntegrityError):
            TopoNode.objects.create(canvas=self.canvas, source='ref', kind='server',
                                    ref_id=server.id, label=server.name)

    def test_self_loop_edge_rejected(self):
        node = TopoNode.objects.create(canvas=self.canvas, source='external',
                                       kind='custom', label='n1')
        with self.assertRaises(IntegrityError):
            TopoEdge.objects.create(canvas=self.canvas, source=node, target=node,
                                    relation='custom')

    def test_edge_across_canvases_rejected_by_clean(self):
        other = TopoCanvas.objects.create(
            name='另一张', slug='other', project_group=self.group,
            channel=self.channel, environment=self.env)
        a = TopoNode.objects.create(canvas=self.canvas, source='external',
                                    kind='custom', label='a')
        b = TopoNode.objects.create(canvas=other, source='external',
                                    kind='custom', label='b')
        edge = TopoEdge(canvas=self.canvas, source=a, target=b, relation='custom')
        with self.assertRaises(ValidationError):
            edge.clean()


class CanvasApiTests(BaseTopoTest):
    def test_crud_and_permission(self):
        """管理员可增删改；无角色的普通用户可查可增（ops_member 回退矩阵）不可改删。"""
        admin = self._client(is_staff=True)
        normal = self._client(is_staff=False)

        payload = {
            'name': '数据库链路', 'slug': 'db-topology',
            'project_group': self.group.id, 'channel': self.channel.id,
            'environment': self.env.id, 'region': 'us',
        }
        r = admin.post('/api/topo/canvases/', payload, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        cid = r.json()['id']

        r = normal.get('/api/topo/canvases/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['data']['total'], 2)

        r = normal.patch(f'/api/topo/canvases/{cid}/', {'name': '改名'}, format='json')
        self.assertEqual(r.status_code, 403)
        r = normal.delete(f'/api/topo/canvases/{cid}/')
        self.assertEqual(r.status_code, 403)

        r = admin.delete(f'/api/topo/canvases/{cid}/')
        self.assertEqual(r.status_code, 204)

    def test_duplicate_slug_in_same_dims_rejected(self):
        admin = self._client()
        r = admin.post('/api/topo/canvases/', {
            'name': '重复', 'slug': 'ingress-path',
            'project_group': self.group.id, 'channel': self.channel.id,
            'environment': self.env.id, 'region': 'us',
        }, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('已存在同标识的画布', r.json()['msg'])

    def test_same_slug_different_dims_allowed(self):
        """同一组合允许多张图，换个环境同名也应放行。"""
        admin = self._client()
        r = admin.post('/api/topo/canvases/', {
            'name': '测试环境同名', 'slug': 'ingress-path',
            'project_group': self.group.id, 'channel': self.channel.id,
            'environment': Environment.objects.get(code='test').id, 'region': 'us',
        }, format='json')
        self.assertEqual(r.status_code, 201, r.content)

    def test_slug_format_validated(self):
        admin = self._client()
        r = admin.post('/api/topo/canvases/', {
            'name': 'x', 'slug': 'Bad Slug!',
            'project_group': self.group.id, 'channel': self.channel.id,
            'environment': self.env.id,
        }, format='json')
        self.assertEqual(r.status_code, 400)

    def test_filters(self):
        admin = self._client()
        r = admin.get('/api/topo/canvases/?project_group=p1105&env=prod&region=us')
        self.assertEqual(r.json()['data']['total'], 1)
        r = admin.get('/api/topo/canvases/?project_group=p1105&region=eu')
        self.assertEqual(r.json()['data']['total'], 0)
        r = admin.get('/api/topo/canvases/?keyword=加速')
        self.assertEqual(r.json()['data']['total'], 1)


class GraphApiTests(BaseTopoTest):
    def _graph_url(self, action='graph'):
        return f'/api/topo/canvases/{self.canvas.id}/{action}/'

    def test_save_and_read_graph(self):
        admin = self._client()
        server = self._server()
        body = {
            'nodes': [
                {'key': 'n1', 'source': 'external', 'kind': 'aws_ga',
                 'label': 'p1105-us-master-ga', 'x': 0, 'y': 0},
                {'key': 'n2', 'source': 'ref', 'kind': 'server', 'ref_id': server.id,
                 'label': '占位会被实时值覆盖', 'x': 280, 'y': 0, 'pinned': True},
            ],
            'edges': [{'source': 'n1', 'target': 'n2', 'relation': 'origin',
                       'label': '源站'}],
        }
        r = admin.put(self._graph_url(), body, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        data = r.json()['data']
        self.assertEqual(data['stats'], {'nodes': 2, 'edges': 1, 'removed_nodes': 0})

        r = admin.get(self._graph_url())
        data = r.json()['data']
        self.assertEqual(len(data['nodes']), 2)
        ref_node = next(n for n in data['nodes'] if n['source'] == 'ref')
        # 引用型节点的展示名以实时值为准，不吃前端传的占位
        self.assertEqual(ref_node['label'], server.name)
        self.assertTrue(ref_node['ref']['exists'])
        self.assertEqual(ref_node['ref']['health'], 'healthy')
        self.assertTrue(ref_node['pinned'])
        self.assertEqual(data['edges'][0]['relation_label'], '源站')

    def test_save_replaces_and_keeps_ids(self):
        """带 id 的节点做更新（主键稳定），未提交的节点连同其连线一并删除。"""
        admin = self._client()
        first = {
            'nodes': [
                {'key': 'a', 'source': 'external', 'kind': 'custom', 'label': 'A'},
                {'key': 'b', 'source': 'external', 'kind': 'custom', 'label': 'B'},
            ],
            'edges': [{'source': 'a', 'target': 'b', 'relation': 'custom'}],
        }
        r = admin.put(self._graph_url(), first, format='json')
        nodes = r.json()['data']['nodes']
        node_a = next(n for n in nodes if n['label'] == 'A')

        second = {
            'nodes': [{'key': 'a', 'id': node_a['id'], 'source': 'external',
                       'kind': 'custom', 'label': 'A 改名', 'x': 99}],
            'edges': [],
        }
        r = admin.put(self._graph_url(), second, format='json')
        data = r.json()['data']
        self.assertEqual(data['stats'], {'nodes': 1, 'edges': 0, 'removed_nodes': 1})
        self.assertEqual(data['nodes'][0]['id'], node_a['id'])
        self.assertEqual(data['nodes'][0]['label'], 'A 改名')
        self.assertEqual(TopoEdge.objects.filter(canvas=self.canvas).count(), 0)

    def test_save_rejects_bad_input(self):
        admin = self._client()
        cases = [
            ({'nodes': [{'source': 'external', 'kind': 'custom', 'label': 'A'}],
              'edges': []}, '缺少 key'),
            ({'nodes': [{'key': 'a', 'source': 'external', 'kind': 'nope',
                         'label': 'A'}], 'edges': []}, '未登记'),
            ({'nodes': [{'key': 'a', 'source': 'ref', 'kind': 'server',
                         'ref_id': 99999, 'label': 'A'}], 'edges': []}, '不存在'),
            ({'nodes': [{'key': 'a', 'source': 'external', 'kind': 'custom',
                         'label': ''}], 'edges': []}, '显示名称不能为空'),
            ({'nodes': [{'key': 'a', 'source': 'external', 'kind': 'custom',
                         'label': 'A'}],
              'edges': [{'source': 'a', 'target': 'ghost'}]}, '不在本次提交的节点里'),
            ({'nodes': [{'key': 'a', 'source': 'external', 'kind': 'custom',
                         'label': 'A'}],
              'edges': [{'source': 'a', 'target': 'a'}]}, '起点与终点相同'),
        ]
        for body, expect in cases:
            with self.subTest(expect=expect):
                r = admin.put(self._graph_url(), body, format='json')
                self.assertEqual(r.status_code, 400, r.content)
                self.assertIn(expect, r.json()['msg'])

    def test_save_rejects_duplicate_ref(self):
        admin = self._client()
        server = self._server()
        body = {'nodes': [
            {'key': 'a', 'source': 'ref', 'kind': 'server', 'ref_id': server.id,
             'label': 'A'},
            {'key': 'b', 'source': 'ref', 'kind': 'server', 'ref_id': server.id,
             'label': 'B'},
        ], 'edges': []}
        r = admin.put(self._graph_url(), body, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('重复引用', r.json()['msg'])

    def test_failed_save_is_rolled_back(self):
        """整图保存是一个事务：后面的节点校验失败，前面的不许留在库里。"""
        admin = self._client()
        body = {'nodes': [
            {'key': 'a', 'source': 'external', 'kind': 'custom', 'label': 'A'},
            {'key': 'b', 'source': 'ref', 'kind': 'server', 'ref_id': 99999,
             'label': 'B'},
        ], 'edges': []}
        r = admin.put(self._graph_url(), body, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertEqual(TopoNode.objects.filter(canvas=self.canvas).count(), 0)

    def test_deleted_entity_shows_as_lost(self):
        """引用的实体被删：节点留在图上并标红，不静默消失。"""
        admin = self._client()
        server = self._server()
        admin.put(self._graph_url(), {'nodes': [
            {'key': 'a', 'source': 'ref', 'kind': 'server', 'ref_id': server.id,
             'label': server.name}], 'edges': []}, format='json')
        server.delete()
        r = admin.get(self._graph_url())
        node = r.json()['data']['nodes'][0]
        self.assertFalse(node['ref']['exists'])
        self.assertEqual(node['ref']['health'], 'danger')
        self.assertEqual(node['label'], 'aws-usw-p1105-01')  # 存的 label 兜底

    def test_normal_user_can_read_but_not_save(self):
        normal = self._client(is_staff=False)
        r = normal.get(self._graph_url())
        self.assertEqual(r.status_code, 200)
        r = normal.put(self._graph_url(), {'nodes': [], 'edges': []}, format='json')
        self.assertEqual(r.status_code, 403)


class AutofillTests(BaseTopoTest):
    def test_autofill_builds_chain_and_is_idempotent(self):
        admin = self._client()
        server = self._server()
        domain, record = self._domain_record(server)
        url = f'/api/topo/canvases/{self.canvas.id}/autofill/'

        r = admin.post(url, {}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        stats = r.json()['data']['stats']
        # 主机 + 解析记录 + 主域名
        self.assertEqual(stats['added_nodes'], 3)
        # 主域名 --解析--> 解析记录 --解析--> 主机
        self.assertEqual(stats['added_edges'], 2)

        kinds = set(TopoNode.objects.filter(canvas=self.canvas)
                    .values_list('kind', flat=True))
        self.assertEqual(kinds, {'server', 'dns_record', 'domain'})

        # 再跑一次不重复生成
        r = admin.post(url, {}, format='json')
        self.assertEqual(r.json()['data']['stats']['added_nodes'], 0)
        self.assertEqual(r.json()['data']['stats']['added_edges'], 0)
        self.assertEqual(TopoNode.objects.filter(canvas=self.canvas).count(), 3)

    def test_autofill_respects_dims_and_region(self):
        admin = self._client()
        self._server(name='in-scope', region='us')
        self._server(name='other-region', region='eu', private_ip='10.0.0.2')
        Server.objects.create(name='other-group', private_ip='10.0.0.3',
                              project_group=ProjectGroup.objects.create(
                                  name='别的组', code='other'),
                              channel=self.channel, environment=self.env, region='us')
        r = admin.post(f'/api/topo/canvases/{self.canvas.id}/autofill/', {},
                       format='json')
        labels = set(TopoNode.objects.filter(canvas=self.canvas, kind='server')
                     .values_list('label', flat=True))
        self.assertEqual(labels, {'in-scope'})
        self.assertEqual(r.json()['data']['stats']['servers'], 1)

    def test_autofill_links_certificate(self):
        admin = self._client()
        server = self._server()
        domain, _ = self._domain_record(server)
        cert = Certificate.objects.create(
            name='wildcard-example', domains=['*.example.com'], status='issued',
            cloud_account=domain.cloud_account,
            expires_at=timezone.now() + timezone.timedelta(days=60))
        CertDeployment.objects.create(certificate=cert, target_type='server',
                                      server=server)
        admin.post(f'/api/topo/canvases/{self.canvas.id}/autofill/', {}, format='json')
        cert_node = TopoNode.objects.get(canvas=self.canvas, kind='certificate')
        self.assertEqual(cert_node.ref_id, cert.id)
        relations = set(TopoEdge.objects.filter(canvas=self.canvas, source=cert_node)
                        .values_list('relation', flat=True))
        # SAN 覆盖主域名 → custom，分发目标命中主机 → landing
        self.assertEqual(relations, {'custom', 'landing'})

    def test_autofill_without_servers(self):
        admin = self._client()
        empty = TopoCanvas.objects.create(
            name='空画布', slug='empty', project_group=self.group,
            channel=self.channel, environment=Environment.objects.get(code='dev'))
        r = admin.post(f'/api/topo/canvases/{empty.id}/autofill/', {}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertIn('还没有服务器', r.json()['msg'])

    def test_autofill_preserves_manual_nodes(self):
        """人手插的 GA 节点不该被自动编排动到。"""
        admin = self._client()
        server = self._server()
        self._domain_record(server)
        ga = TopoNode.objects.create(canvas=self.canvas, source='external',
                                     kind='aws_ga', label='p1105-us-ga',
                                     x=560, y=0, pinned=True)
        admin.post(f'/api/topo/canvases/{self.canvas.id}/autofill/', {}, format='json')
        ga.refresh_from_db()
        self.assertEqual((ga.x, ga.y, ga.pinned), (560, 0, True))


class CandidatesAndMetaTests(BaseTopoTest):
    def test_candidates_narrowed_and_marked(self):
        admin = self._client()
        server = self._server()
        self._server(name='out-of-scope', region='eu', private_ip='10.0.0.9')
        url = f'/api/topo/canvases/{self.canvas.id}/candidates/'

        r = admin.get(f'{url}?kind=server')
        results = r.json()['data']['results']
        self.assertEqual([i['label'] for i in results], [server.name])
        self.assertFalse(results[0]['on_canvas'])

        TopoNode.objects.create(canvas=self.canvas, source='ref', kind='server',
                                ref_id=server.id, label=server.name)
        r = admin.get(f'{url}?kind=server')
        self.assertTrue(r.json()['data']['results'][0]['on_canvas'])

    def test_candidates_other_kinds(self):
        admin = self._client()
        server = self._server()
        domain, record = self._domain_record(server)
        url = f'/api/topo/canvases/{self.canvas.id}/candidates/'
        for kind, expect in (('domain', domain.name), ('dns_record', record.fqdn)):
            with self.subTest(kind=kind):
                r = admin.get(f'{url}?kind={kind}')
                self.assertIn(expect, [i['label'] for i in r.json()['data']['results']])

    def test_candidates_rejects_unknown_kind(self):
        admin = self._client()
        r = admin.get(f'/api/topo/canvases/{self.canvas.id}/candidates/?kind=aws_ga')
        self.assertEqual(r.status_code, 400)
        self.assertIn('不是可引用的平台资源类型', r.json()['msg'])

    def test_meta_drives_palette(self):
        admin = self._client()
        r = admin.get('/api/topo/meta/')
        data = r.json()['data']
        self.assertIn('server', [i['kind'] for i in data['ref_kinds']])
        groups = {g['group'] for g in data['external_groups']}
        self.assertTrue({'AWS', 'GCP', '腾讯云', '阿里云'} <= groups)
        self.assertIn('resolve', [i['value'] for i in data['relations']])

    def test_meta_requires_login(self):
        r = APIClient().get('/api/topo/meta/')
        self.assertEqual(r.status_code, 401)


class DimScopeTests(BaseTopoTest):
    def test_canvas_out_of_scope_is_hidden_and_blocked(self):
        """配了数据范围的账号：范围外的画布列表看不到，直接猜 id 也进不去。"""
        user = User.objects.create(username='scoped_t', is_staff=True)
        user.set_password('Pass!2345xyz')
        user.save()
        other_group = ProjectGroup.objects.create(name='别的组', code='other')
        user.allowed_project_groups.add(other_group)
        c = APIClient()
        r = c.post('/api/auth/login/',
                   {'username': 'scoped_t', 'password': 'Pass!2345xyz'}, format='json')
        c.credentials(HTTP_AUTHORIZATION='Bearer ' + r.json()['access'])

        r = c.get('/api/topo/canvases/')
        self.assertEqual(r.json()['data']['total'], 0)
        r = c.get(f'/api/topo/canvases/{self.canvas.id}/graph/')
        self.assertEqual(r.status_code, 404)


class RoleMatrixTests(TestCase):
    def test_topo_module_registered_and_granted(self):
        """新模块必须在注册表与内置角色矩阵里同时出现，否则页面永远配不到它。"""
        from apps.auth_mgr import roles
        from apps.auth_mgr.models import Role

        self.assertIn('topo', roles.MODULES)
        self.assertEqual(roles.MODULE_GROUPS[0][1], ('cmdb', 'dict', 'topo'))
        self.assertEqual(Role.objects.get(code='super').level_for('topo'), 'write')
        self.assertEqual(Role.objects.get(code='ops_admin').level_for('topo'), 'write')
        self.assertEqual(Role.objects.get(code='ops_member').level_for('topo'),
                         'read_create')
