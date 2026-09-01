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


class DimsEnforcementTests(BaseTopoTest):
    def test_graph_save_rejects_out_of_dim_server(self):
        """维度不符的主机不许挂到画布上：dev 画布上画着生产机器比没有图更危险。"""
        admin = self._client()
        other = self._server(name='eu-host', region='eu', private_ip='10.9.9.9')
        r = admin.put(f'/api/topo/canvases/{self.canvas.id}/graph/', {
            'nodes': [{'key': 'a', 'source': 'ref', 'kind': 'server',
                       'ref_id': other.id, 'label': other.name}],
            'edges': [],
        }, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('不属于本画布的维度', r.json()['msg'])

    def test_graph_save_allows_dimensionless_kinds(self):
        """域名没有项目组字段，不做维度判定（按名字猜属于哪个项目会猜错）。"""
        admin = self._client()
        server = self._server()
        domain, _ = self._domain_record(server)
        r = admin.put(f'/api/topo/canvases/{self.canvas.id}/graph/', {
            'nodes': [{'key': 'd', 'source': 'ref', 'kind': 'domain',
                       'ref_id': domain.id, 'label': domain.name}],
            'edges': [],
        }, format='json')
        self.assertEqual(r.status_code, 200, r.content)


class ImpactTests(BaseTopoTest):
    def _put_chain(self, client, canvas, server, domain):
        return client.put(f'/api/topo/canvases/{canvas.id}/graph/', {
            'nodes': [
                {'key': 'd', 'source': 'ref', 'kind': 'domain', 'ref_id': domain.id,
                 'label': domain.name},
                {'key': 'ga', 'source': 'external', 'kind': 'aws_ga', 'label': 'p1105-ga'},
                {'key': 's', 'source': 'ref', 'kind': 'server', 'ref_id': server.id,
                 'label': server.name},
            ],
            'edges': [
                {'source': 'd', 'target': 'ga', 'relation': 'resolve'},
                {'source': 'ga', 'target': 's', 'relation': 'origin'},
            ],
        }, format='json')

    def test_impact_lists_canvases_with_neighbours(self):
        admin = self._client()
        server = self._server()
        domain, _ = self._domain_record(server)
        r = self._put_chain(admin, self.canvas, server, domain)
        self.assertEqual(r.status_code, 200, r.content)

        r = admin.get(f'/api/topo/impact/?kind=server&ref_id={server.id}')
        self.assertEqual(r.status_code, 200)
        data = r.json()['data']
        self.assertEqual(data['label'], server.name)
        self.assertEqual(data['total'], 1)
        hit = data['results'][0]
        self.assertEqual(hit['canvas']['id'], self.canvas.id)
        self.assertEqual(hit['canvas']['link']['path'], f'/topo/canvases/{self.canvas.id}')
        # 上游 = 指向它的（GA 的流量落到这台机上），下游为空
        self.assertEqual([u['node']['label'] for u in hit['upstream']], ['p1105-ga'])
        self.assertEqual(hit['upstream'][0]['relation_label'], '源站')
        self.assertEqual(hit['downstream'], [])

    def test_impact_covers_multiple_canvases(self):
        admin = self._client()
        server = self._server()
        domain, _ = self._domain_record(server)
        second = TopoCanvas.objects.create(
            name='第二张', slug='second', project_group=self.group,
            channel=self.channel, environment=self.env, region='us')
        self._put_chain(admin, self.canvas, server, domain)
        self._put_chain(admin, second, server, domain)
        r = admin.get(f'/api/topo/impact/?kind=server&ref_id={server.id}')
        self.assertEqual(r.json()['data']['total'], 2)

    def test_impact_neighbour_uses_live_state(self):
        """邻居节点的名称与状态口径必须与画布一致（同一套 resolve_refs）。"""
        admin = self._client()
        server = self._server()
        domain, _ = self._domain_record(server)
        self._put_chain(admin, self.canvas, server, domain)
        r = admin.get(f'/api/topo/impact/?kind=domain&ref_id={domain.id}')
        hit = r.json()['data']['results'][0]
        self.assertEqual([d['node']['label'] for d in hit['downstream']], ['p1105-ga'])
        self.assertEqual(hit['upstream'], [])

    def test_impact_respects_data_scope(self):
        admin = self._client()
        server = self._server()
        domain, _ = self._domain_record(server)
        self._put_chain(admin, self.canvas, server, domain)

        scoped = User.objects.create(username='scoped_i', is_staff=True)
        scoped.set_password('Pass!2345xyz')
        scoped.save()
        scoped.allowed_project_groups.add(ProjectGroup.objects.create(
            name='别的组', code='other-i'))
        c = APIClient()
        res = c.post('/api/auth/login/',
                     {'username': 'scoped_i', 'password': 'Pass!2345xyz'}, format='json')
        c.credentials(HTTP_AUTHORIZATION='Bearer ' + res.json()['access'])
        r = c.get(f'/api/topo/impact/?kind=server&ref_id={server.id}')
        self.assertEqual(r.json()['data']['total'], 0)

    def test_impact_validates_params(self):
        admin = self._client()
        r = admin.get('/api/topo/impact/?kind=aws_ga&ref_id=1')
        self.assertEqual(r.status_code, 400)
        self.assertIn('不是可引用的平台资源类型', r.json()['msg'])
        r = admin.get('/api/topo/impact/?kind=server&ref_id=abc')
        self.assertEqual(r.status_code, 400)

    def test_impact_of_deleted_entity(self):
        admin = self._client()
        r = admin.get('/api/topo/impact/?kind=server&ref_id=99999')
        data = r.json()['data']
        self.assertIsNone(data['label'])
        self.assertEqual(data['total'], 0)


class ExportImportTests(BaseTopoTest):
    def _seed_graph(self, client, canvas=None):
        canvas = canvas or self.canvas
        server = self._server()
        domain, record = self._domain_record(server)
        r = client.put(f'/api/topo/canvases/{canvas.id}/graph/', {
            'nodes': [
                {'key': 'd', 'source': 'ref', 'kind': 'domain', 'ref_id': domain.id,
                 'label': domain.name, 'x': 0, 'y': 0},
                {'key': 'ga', 'source': 'external', 'kind': 'aws_ga',
                 'label': 'p1105-us-ga', 'x': 280, 'y': 0, 'pinned': True},
                {'key': 's', 'source': 'ref', 'kind': 'server', 'ref_id': server.id,
                 'label': server.name, 'x': 560, 'y': 0},
            ],
            'edges': [
                {'source': 'd', 'target': 'ga', 'relation': 'resolve'},
                {'source': 'ga', 'target': 's', 'relation': 'origin'},
            ],
        }, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        return server, domain, record

    def test_export_uses_natural_keys_and_codes(self):
        admin = self._client()
        server, domain, _ = self._seed_graph(admin)
        r = admin.get(f'/api/topo/canvases/{self.canvas.id}/export/')
        self.assertEqual(r.status_code, 200)
        data = r.json()['data']
        self.assertEqual(data['version'], 1)
        # 维度用 code，不是 id：换一台实例 id 一定不同
        self.assertEqual(data['canvas']['project_group'], 'p1105')
        self.assertEqual(data['canvas']['environment'], 'prod')
        refs = {n['kind']: n['ref'] for n in data['nodes'] if n['source'] == 'ref'}
        self.assertEqual(refs['server'], {'name': server.name})
        self.assertEqual(refs['domain'], {'name': domain.name})
        external = next(n for n in data['nodes'] if n['source'] == 'external')
        self.assertTrue(external['pinned'])
        self.assertEqual(len(data['edges']), 2)

    def test_import_creates_canvas(self):
        admin = self._client()
        self._seed_graph(admin)
        payload = admin.get(f'/api/topo/canvases/{self.canvas.id}/export/').json()['data']
        r = admin.post('/api/topo/import/', {
            'payload': payload,
            'overrides': {'name': '导入的图', 'slug': 'imported'},
        }, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        data = r.json()['data']
        self.assertEqual(data['canvas']['slug'], 'imported')
        self.assertEqual(data['report']['nodes'], 3)
        self.assertEqual(data['report']['edges'], 2)
        self.assertEqual(data['report']['skipped_nodes'], [])

    def test_import_conflict_requires_overwrite(self):
        admin = self._client()
        self._seed_graph(admin)
        payload = admin.get(f'/api/topo/canvases/{self.canvas.id}/export/').json()['data']
        r = admin.post('/api/topo/import/', {'payload': payload}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('已存在标识为', r.json()['msg'])

        r = admin.post('/api/topo/import/',
                       {'payload': payload, 'overwrite': True}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.json()['data']['report']['overwritten'])

    def test_import_overwrite_needs_write_level(self):
        """新建一张图是 read_create 的正常动作，把别人那张整份冲掉不是。"""
        admin = self._client()
        self._seed_graph(admin)
        payload = admin.get(f'/api/topo/canvases/{self.canvas.id}/export/').json()['data']
        normal = self._client(is_staff=False)
        r = normal.post('/api/topo/import/',
                        {'payload': payload, 'overwrite': True}, format='json')
        self.assertEqual(r.status_code, 403)
        # 不覆盖、换个标识则允许（ops_member 回退矩阵是 read_create）
        r = normal.post('/api/topo/import/', {
            'payload': payload, 'overrides': {'name': '普通用户建的', 'slug': 'by-member'},
        }, format='json')
        self.assertEqual(r.status_code, 200, r.content)

    def test_import_skips_unresolvable_and_out_of_dim_refs(self):
        admin = self._client()
        self._seed_graph(admin)
        payload = admin.get(f'/api/topo/canvases/{self.canvas.id}/export/').json()['data']
        # 导到另一个环境：主机维度不符会被跳过，外部节点与域名照常落地
        dev = Environment.objects.get(code='dev')
        r = admin.post('/api/topo/import/', {
            'payload': payload,
            'overrides': {'name': 'dev 链路', 'slug': 'dev-path', 'environment': dev.id},
        }, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        report = r.json()['data']['report']
        self.assertEqual(report['nodes'], 2)          # 域名 + GA
        self.assertEqual(len(report['skipped_nodes']), 1)
        self.assertIn('维度', report['skipped_nodes'][0]['reason'])
        self.assertEqual(report['skipped_edges'], 1)  # GA → 主机 那条端点没了

    def test_import_reports_missing_resource(self):
        admin = self._client()
        self._seed_graph(admin)
        payload = admin.get(f'/api/topo/canvases/{self.canvas.id}/export/').json()['data']
        for node in payload['nodes']:
            if node['kind'] == 'domain':
                node['ref'] = {'name': 'not-here.example.com'}
        r = admin.post('/api/topo/import/', {
            'payload': payload, 'overrides': {'name': 'x', 'slug': 'missing-ref'},
        }, format='json')
        report = r.json()['data']['report']
        self.assertEqual([s['reason'] for s in report['skipped_nodes']],
                         ['本实例找不到同名资源'])

    def test_import_rejects_unknown_dict_code_and_bad_payload(self):
        admin = self._client()
        self._seed_graph(admin)
        payload = admin.get(f'/api/topo/canvases/{self.canvas.id}/export/').json()['data']
        broken = {**payload, 'canvas': {**payload['canvas'], 'project_group': 'ghost'}}
        r = admin.post('/api/topo/import/', {'payload': broken}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('本实例没有项目组', r.json()['msg'])

        cases = [
            ({'payload': {'version': 99, 'nodes': [{}]}}, '不认识的导出格式版本'),
            ({'payload': {'nodes': []}}, '没有任何节点'),
            ({}, '缺少 payload'),
        ]
        for body, expect in cases:
            with self.subTest(expect=expect):
                r = admin.post('/api/topo/import/', body, format='json')
                self.assertEqual(r.status_code, 400)
                self.assertIn(expect, r.json()['msg'])

    def test_import_all_nodes_unresolvable_is_rejected(self):
        """一个节点都落不了地时不留一张空画布。"""
        admin = self._client()
        r = admin.post('/api/topo/import/', {'payload': {
            'canvas': {'name': '空的', 'slug': 'ghost-only',
                       'project_group': 'p1105', 'channel': 'starunion',
                       'environment': 'prod'},
            'nodes': [{'key': 'a', 'source': 'ref', 'kind': 'server',
                       'label': 'ghost', 'ref': {'name': 'ghost'}}],
            'edges': [],
        }}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('全部无法落地', r.json()['msg'])
        self.assertFalse(TopoCanvas.objects.filter(slug='ghost-only').exists())


class CloneTests(BaseTopoTest):
    def setUp(self):
        super().setUp()
        self.admin = self._client()
        self.server = self._server()
        self.domain, _ = self._domain_record(self.server)
        self.admin.put(f'/api/topo/canvases/{self.canvas.id}/graph/', {
            'nodes': [
                {'key': 'ga', 'source': 'external', 'kind': 'aws_ga', 'label': 'ga-1'},
                {'key': 'lb', 'source': 'external', 'kind': 'aws_alb', 'label': 'alb-1'},
                {'key': 's', 'source': 'ref', 'kind': 'server', 'ref_id': self.server.id,
                 'label': self.server.name},
            ],
            'edges': [
                {'source': 'ga', 'target': 'lb', 'relation': 'backend'},
                {'source': 'lb', 'target': 's', 'relation': 'origin'},
            ],
        }, format='json')

    def test_clone_to_another_env_keeps_skeleton(self):
        """克隆到 dev：GA/LB 骨架与它们之间的连线保留，生产主机跳过并报告。"""
        dev = Environment.objects.get(code='dev')
        r = self.admin.post(f'/api/topo/canvases/{self.canvas.id}/clone/', {
            'name': 'dev 加速链路', 'slug': 'ingress-path-dev', 'environment': dev.id,
        }, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        data = r.json()['data']
        self.assertEqual(data['canvas']['environment_code'], 'dev')
        self.assertEqual(data['report']['nodes'], 2)
        self.assertEqual(data['report']['edges'], 1)
        self.assertEqual(len(data['report']['skipped_nodes']), 1)
        self.assertIn('跳过', r.json()['msg'])
        # 原画布不受影响
        self.assertEqual(TopoNode.objects.filter(canvas=self.canvas).count(), 3)

    def test_clone_same_dims_needs_new_slug(self):
        r = self.admin.post(f'/api/topo/canvases/{self.canvas.id}/clone/', {},
                            format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('必须指定新的画布名称或标识', r.json()['msg'])

        r = self.admin.post(f'/api/topo/canvases/{self.canvas.id}/clone/',
                            {'name': '副本'}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('已存在标识为', r.json()['msg'])

    def test_clone_keeps_refs_when_dims_unchanged(self):
        """同维度另存一张：主机也留着（维度是对得上的）。"""
        r = self.admin.post(f'/api/topo/canvases/{self.canvas.id}/clone/',
                            {'name': '副本', 'slug': 'ingress-path-copy'}, format='json')
        self.assertEqual(r.json()['data']['report']['nodes'], 3)
        self.assertEqual(r.json()['data']['report']['skipped_nodes'], [])

    def test_clone_target_dims_must_be_in_scope(self):
        """范围内的图不许被克隆成一张范围外的图。"""
        other_group = ProjectGroup.objects.create(name='别的组', code='other-c')
        user = User.objects.create(username='scoped_c', is_staff=True)
        user.set_password('Pass!2345xyz')
        user.save()
        user.allowed_project_groups.add(self.group)
        c = APIClient()
        res = c.post('/api/auth/login/',
                     {'username': 'scoped_c', 'password': 'Pass!2345xyz'}, format='json')
        c.credentials(HTTP_AUTHORIZATION='Bearer ' + res.json()['access'])
        r = c.post(f'/api/topo/canvases/{self.canvas.id}/clone/', {
            'name': '越界副本', 'slug': 'out-of-scope', 'project_group': other_group.id,
        }, format='json')
        self.assertEqual(r.status_code, 403)
        self.assertIn('数据范围', r.json()['msg'])


class HealthTests(BaseTopoTest):
    def _put(self, client, nodes, edges=None):
        return client.put(f'/api/topo/canvases/{self.canvas.id}/graph/',
                          {'nodes': nodes, 'edges': edges or []}, format='json')

    def test_server_issues_cover_status_and_agent(self):
        """执行通道也算健康度：Agent 离线的主机在图上看着好好的，命令根本下不去。"""
        from apps.agent_mgr.models import AgentNode

        admin = self._client()
        agent = AgentNode.objects.create(name='nat-agent', status='offline')
        server = self._server(status='maintenance', channel_mode='agent', agent=agent)
        r = self._put(admin, [{'key': 's', 'source': 'ref', 'kind': 'server',
                               'ref_id': server.id, 'label': server.name}])
        self.assertEqual(r.status_code, 200, r.content)
        node = r.json()['data']['nodes'][0]
        texts = [i['text'] for i in node['ref']['issues']]
        self.assertEqual(texts, ['主机维护中', '执行通道 Agent「nat-agent」离线'])
        # 最坏一条定颜色：维护中是黄的，但 Agent 离线是红的
        self.assertEqual(node['ref']['health'], 'danger')

    def test_disabled_dns_record_is_flagged(self):
        """图上看着通、实际这条解析是关的：不标出来会照着图排查半天。"""
        admin = self._client()
        server = self._server()
        _, record = self._domain_record(server)
        record.enabled = False
        record.sync_status = 'synced'
        record.save(update_fields=['enabled', 'sync_status'])
        r = self._put(admin, [{'key': 'r', 'source': 'ref', 'kind': 'dns_record',
                               'ref_id': record.id, 'label': record.fqdn}])
        node = r.json()['data']['nodes'][0]
        self.assertEqual([i['text'] for i in node['ref']['issues']], ['解析记录已停用'])
        self.assertEqual(node['ref']['health'], 'warning')

    def test_healthy_server_has_no_issues(self):
        admin = self._client()
        server = self._server()
        r = self._put(admin, [{'key': 's', 'source': 'ref', 'kind': 'server',
                               'ref_id': server.id, 'label': server.name}])
        node = r.json()['data']['nodes'][0]
        self.assertEqual(node['ref']['issues'], [])
        self.assertEqual(node['ref']['health'], 'healthy')

    def test_health_summary_counts_and_sorts(self):
        admin = self._client()
        ok_server = self._server(name='ok-host')
        bad_server = self._server(name='bad-host', private_ip='10.0.0.5',
                                  status='stopped')
        self._put(admin, [
            {'key': 'a', 'source': 'ref', 'kind': 'server', 'ref_id': ok_server.id,
             'label': ok_server.name},
            {'key': 'b', 'source': 'ref', 'kind': 'server', 'ref_id': bad_server.id,
             'label': bad_server.name},
            {'key': 'x', 'source': 'external', 'kind': 'aws_ga', 'label': 'ga'},
        ])
        clean = TopoCanvas.objects.create(
            name='干净的', slug='clean', project_group=self.group,
            channel=self.channel, environment=self.env, region='us')

        r = admin.get('/api/topo/health/')
        self.assertEqual(r.status_code, 200)
        data = r.json()['data']
        self.assertEqual(data['danger'], 1)
        # 有异常的排前面
        self.assertEqual(data['results'][0]['canvas']['id'], self.canvas.id)
        first = data['results'][0]
        # 外部节点既不算健康也不算异常：平台不知道它们的真实状态
        self.assertEqual(first['total'], 2)
        self.assertEqual((first['healthy'], first['danger']), (1, 1))
        self.assertEqual([i['text'] for i in first['issues']], ['主机已停止'])
        self.assertEqual(data['results'][-1]['canvas']['id'], clean.id)

        r = admin.get('/api/topo/health/?only_issues=true')
        self.assertEqual(r.json()['data']['total'], 1)

    def test_health_counts_lost_refs(self):
        admin = self._client()
        server = self._server()
        self._put(admin, [{'key': 's', 'source': 'ref', 'kind': 'server',
                           'ref_id': server.id, 'label': server.name}])
        server.delete()
        r = admin.get('/api/topo/health/')
        bucket = r.json()['data']['results'][0]
        self.assertEqual((bucket['lost'], bucket['danger']), (1, 1))
        self.assertEqual(bucket['issues'][0]['text'], '引用的资源已不存在')

    def test_health_respects_data_scope(self):
        admin = self._client()
        server = self._server(status='stopped')
        self._put(admin, [{'key': 's', 'source': 'ref', 'kind': 'server',
                           'ref_id': server.id, 'label': server.name}])
        scoped = User.objects.create(username='scoped_h', is_staff=True)
        scoped.set_password('Pass!2345xyz')
        scoped.save()
        scoped.allowed_project_groups.add(ProjectGroup.objects.create(
            name='别的组', code='other-h'))
        c = APIClient()
        res = c.post('/api/auth/login/',
                     {'username': 'scoped_h', 'password': 'Pass!2345xyz'}, format='json')
        c.credentials(HTTP_AUTHORIZATION='Bearer ' + res.json()['access'])
        self.assertEqual(c.get('/api/topo/health/').json()['data']['total'], 0)

    def test_dashboard_surfaces_topo_alert(self):
        """概览页的「拓扑异常」块与画布上的颜色同源，点进去就是那张图。"""
        admin = self._client()
        server = self._server(status='stopped')
        self._put(admin, [{'key': 's', 'source': 'ref', 'kind': 'server',
                           'ref_id': server.id, 'label': server.name}])
        r = admin.get('/api/dashboard/overview/')
        self.assertEqual(r.status_code, 200)
        block = next(b for b in r.json()['data']['alerts'] if b['key'] == 'topo_issues')
        self.assertEqual(block['level'], 'danger')
        self.assertEqual(block['total'], 1)
        self.assertEqual(block['link']['path'], '/topo/canvases')
        self.assertEqual(block['items'][0]['link']['path'],
                         f'/topo/canvases/{self.canvas.id}')
        self.assertIn('主机已停止', block['items'][0]['detail'])

    def test_dashboard_hides_block_without_topo_permission(self):
        """块级权限在后端裁：没有 topo 权限的账号不该从首页读到画布名。"""
        from apps.auth_mgr.models import Role

        admin = self._client()
        server = self._server(status='stopped')
        self._put(admin, [{'key': 's', 'source': 'ref', 'kind': 'server',
                           'ref_id': server.id, 'label': server.name}])
        role = Role.objects.create(name='无拓扑', code='no-topo',
                                   permissions={'cmdb': 'read', 'topo': 'none'})
        user = User.objects.create(username='no_topo', is_staff=False, role=role)
        user.set_password('Pass!2345xyz')
        user.save()
        c = APIClient()
        res = c.post('/api/auth/login/',
                     {'username': 'no_topo', 'password': 'Pass!2345xyz'}, format='json')
        c.credentials(HTTP_AUTHORIZATION='Bearer ' + res.json()['access'])
        r = c.get('/api/dashboard/overview/')
        keys = [b['key'] for b in r.json()['data']['alerts']]
        self.assertNotIn('topo_issues', keys)


class DashboardTopoLinkTests(BaseTopoTest):
    """概览页异常明细上的「所在链路图」深链。"""

    def _put(self, client, nodes):
        r = client.put(f'/api/topo/canvases/{self.canvas.id}/graph/',
                       {'nodes': nodes, 'edges': []}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        return r

    def _block(self, client, key):
        r = client.get('/api/dashboard/overview/')
        self.assertEqual(r.status_code, 200)
        blocks = r.json()['data']['alerts']
        return next((b for b in blocks if b['key'] == key), None)

    def _expiring_cert(self, name='soon-expire'):
        account = CloudAccount.objects.create(
            name=f'账号-{name}', provider=self.provider, access_key='AK')
        return Certificate.objects.create(
            name=name, domains=['a.example.com'], status='issued',
            cloud_account=account,
            expires_at=timezone.now() + timezone.timedelta(days=5))

    def test_certificate_alert_carries_canvas_link(self):
        admin = self._client()
        cert = self._expiring_cert()
        self._put(admin, [{'key': 'c', 'source': 'ref', 'kind': 'certificate',
                           'ref_id': cert.id, 'label': cert.name}])
        block = self._block(admin, 'cert_expiring')
        item = next(i for i in block['items'] if i['id'] == cert.id)
        self.assertEqual([c['id'] for c in item['topo']], [self.canvas.id])
        self.assertEqual(item['topo'][0]['link']['path'],
                         f'/topo/canvases/{self.canvas.id}')
        # 生产环境的画布标红，值班的人先看这个
        self.assertTrue(item['topo'][0]['is_prod'])

    def test_agent_alert_points_at_canvases_of_its_hosts(self):
        """Agent 本身不是画布节点，但钉死到它的主机是——那才是值班要知道的。"""
        from apps.agent_mgr.models import AgentNode

        admin = self._client()
        agent = AgentNode.objects.create(name='nat-1', status='offline', enabled=True)
        server = self._server(channel_mode='agent', agent=agent)
        self._put(admin, [{'key': 's', 'source': 'ref', 'kind': 'server',
                           'ref_id': server.id, 'label': server.name}])
        block = self._block(admin, 'agent_offline')
        item = next(i for i in block['items'] if i['id'] == agent.id)
        self.assertEqual([c['id'] for c in item['topo']], [self.canvas.id])

    def test_dns_failure_falls_back_to_domain_node(self):
        """图上只画了主域名、没画这条记录时也要能跳过去。"""
        admin = self._client()
        server = self._server()
        domain, record = self._domain_record(server)
        record.sync_status = 'failed'
        record.save(update_fields=['sync_status'])
        self._put(admin, [{'key': 'd', 'source': 'ref', 'kind': 'domain',
                           'ref_id': domain.id, 'label': domain.name}])
        block = self._block(admin, 'dns_sync_failed')
        item = next(i for i in block['items'] if i['id'] == record.id)
        self.assertEqual([c['id'] for c in item['topo']], [self.canvas.id])

    def test_no_link_without_topo_permission(self):
        from apps.auth_mgr.models import Role

        admin = self._client()
        cert = self._expiring_cert()
        self._put(admin, [{'key': 'c', 'source': 'ref', 'kind': 'certificate',
                           'ref_id': cert.id, 'label': cert.name}])
        role = Role.objects.create(name='只看云控', code='cloud-only',
                                   permissions={'cloud': 'read', 'topo': 'none'})
        user = User.objects.create(username='cloud_only', is_staff=False, role=role)
        user.set_password('Pass!2345xyz')
        user.save()
        c = APIClient()
        res = c.post('/api/auth/login/',
                     {'username': 'cloud_only', 'password': 'Pass!2345xyz'}, format='json')
        c.credentials(HTTP_AUTHORIZATION='Bearer ' + res.json()['access'])
        block = self._block(c, 'cert_expiring')
        self.assertIsNotNone(block)
        self.assertNotIn('topo', block['items'][0])

    def test_link_respects_data_scope(self):
        """范围外的画布不该借概览页的深链露出来。"""
        admin = self._client()
        cert = self._expiring_cert()
        self._put(admin, [{'key': 'c', 'source': 'ref', 'kind': 'certificate',
                           'ref_id': cert.id, 'label': cert.name}])
        scoped = User.objects.create(username='scoped_d', is_staff=True)
        scoped.set_password('Pass!2345xyz')
        scoped.save()
        scoped.allowed_project_groups.add(ProjectGroup.objects.create(
            name='别的组', code='other-d'))
        c = APIClient()
        res = c.post('/api/auth/login/',
                     {'username': 'scoped_d', 'password': 'Pass!2345xyz'}, format='json')
        c.credentials(HTTP_AUTHORIZATION='Bearer ' + res.json()['access'])
        block = self._block(c, 'cert_expiring')
        self.assertNotIn('topo', block['items'][0])

    def test_items_without_canvas_have_no_topo_key(self):
        """没画进任何图的异常不带这个字段，前端就不会渲染一个空壳标签。"""
        admin = self._client()
        self._expiring_cert('nowhere')
        block = self._block(admin, 'cert_expiring')
        self.assertNotIn('topo', block['items'][0])


class NodeAttrsTests(BaseTopoTest):
    def test_kind_label_override_survives_round_trip(self):
        """前端把自定义类型标签存在 attrs.kind_label 里，后端原样收发。

        这一条锁住的是「不需要为展示标签加一列」这个决定：attrs 已经是自由字段，
        加列意味着迁移 + 序列化器 + 导出格式一起动，而这个值不参与任何判定。
        """
        admin = self._client()
        url = f'/api/topo/canvases/{self.canvas.id}/graph/'
        r = admin.put(url, {'nodes': [{
            'key': 'ga', 'source': 'external', 'kind': 'aws_ga', 'label': 'p1105-ga',
            'attrs': {'kind_label': 'AWS · 全球加速'},
        }], 'edges': []}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        node = r.json()['data']['nodes'][0]
        self.assertEqual(node['attrs'], {'kind_label': 'AWS · 全球加速'})
        # kind_label 字段本身仍是登记表给的默认名，真实类型不受影响
        self.assertEqual(node['kind_label'], 'GA 加速器')
        self.assertEqual(node['kind'], 'aws_ga')

        node = TopoNode.objects.get(canvas=self.canvas)
        self.assertEqual(node.attrs['kind_label'], 'AWS · 全球加速')

    def test_export_carries_attrs(self):
        """导出/克隆也要带着它，否则换个维度重画一遍标签全丢。"""
        admin = self._client()
        admin.put(f'/api/topo/canvases/{self.canvas.id}/graph/', {'nodes': [{
            'key': 'ga', 'source': 'external', 'kind': 'aws_ga', 'label': 'p1105-ga',
            'attrs': {'kind_label': 'AWS · 全球加速'},
        }], 'edges': []}, format='json')
        r = admin.get(f'/api/topo/canvases/{self.canvas.id}/export/')
        self.assertEqual(r.json()['data']['nodes'][0]['attrs'],
                         {'kind_label': 'AWS · 全球加速'})


class ServerServiceLabelTests(BaseTopoTest):
    """节点卡片上的服务标签取数口径（asset_mgr.ServerService，画布只读）。"""

    def _service(self, server, name, code, status='deployed', order=0, count=1,
                 process_name=''):
        from apps.asset_mgr.models import ServerService, ServiceType

        service_type = ServiceType.objects.create(name=name, code=code, sort_order=order)
        return ServerService.objects.create(
            server=server, service_type=service_type, status=status,
            process_count=count, process_name=process_name)

    def _node(self, client, server):
        r = client.put(f'/api/topo/canvases/{self.canvas.id}/graph/', {
            'nodes': [{'key': 's', 'source': 'ref', 'kind': 'server',
                       'ref_id': server.id, 'label': server.name}],
            'edges': [],
        }, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()['data']['nodes'][0]

    def test_services_are_structured_and_sorted(self):
        """已部署在前，同状态按服务类型字典排序；带进程名与进程数。"""
        admin = self._client()
        server = self._server()
        self._service(server, '网关', 'gateway', 'deployed', order=2, count=1,
                      process_name='gate_server')
        self._service(server, '战斗服', 'battle', 'deployed', order=1, count=4,
                      process_name='battle_server')
        self._service(server, '跨服', 'cross', 'planned', order=3, count=2)
        node = self._node(admin, server)
        services = node['ref']['extra']['services']
        self.assertEqual([s['name'] for s in services], ['战斗服', '网关', '跨服'])
        self.assertEqual(services[0], {
            'name': '战斗服', 'code': 'battle', 'status': 'deployed',
            'status_label': '已部署', 'process_name': 'battle_server',
            'process_count': 4,
        })
        # planned 也上图：区分「已部上」与「规划了还没部」正是画链路时要核对的
        self.assertEqual(services[-1]['status'], 'planned')

    def test_offline_services_excluded(self):
        """已下线的服务留在链路图上是噪音。"""
        admin = self._client()
        server = self._server()
        self._service(server, '老战斗服', 'battle-old', 'offline')
        self._service(server, '战斗服', 'battle', 'deployed')
        node = self._node(admin, server)
        self.assertEqual([s['code'] for s in node['ref']['extra']['services']],
                         ['battle'])

    def test_planned_services_do_not_affect_health(self):
        """规划缺口是计划问题不是运行故障：新规划的机器不该一上图就黄。"""
        admin = self._client()
        server = self._server()
        self._service(server, '战斗服', 'battle', 'planned')
        node = self._node(admin, server)
        self.assertEqual(node['ref']['health'], 'healthy')
        self.assertEqual(node['ref']['issues'], [])

    def test_service_list_is_live_not_snapshot(self):
        """服务改了状态，图上刷一次就跟着变——不存快照。"""
        admin = self._client()
        server = self._server()
        service = self._service(server, '战斗服', 'battle', 'planned')
        self._node(admin, server)
        service.status = 'deployed'
        service.save(update_fields=['status'])
        r = admin.get(f'/api/topo/canvases/{self.canvas.id}/graph/')
        services = r.json()['data']['nodes'][0]['ref']['extra']['services']
        self.assertEqual(services[0]['status'], 'deployed')
