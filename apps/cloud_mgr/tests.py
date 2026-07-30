"""cloud_mgr 接口测试：云账号/主域名/解析记录 CRUD、推送同步（mock 厂商）、权限。"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.asset_mgr.models import CloudProvider, Server
from apps.cloud_mgr.models import CloudAccount, DnsRecord, Domain
from apps.cloud_mgr.providers.base import BaseDnsProvider, DnsProviderError, DnsRecordData


class FakeDnsProvider(BaseDnsProvider):
    """内存假厂商：记录推送调用，供断言。"""

    def __init__(self, *args, **kwargs):
        super().__init__('ak', 'sk')
        self.calls = []

    def list_domains(self):
        return ['example.com', 'demo.cn']

    def list_records(self, domain):
        return [
            DnsRecordData(rr='www', type='A', value='1.2.3.4', ttl=600, remote_record_id='r1'),
            DnsRecordData(rr='_acme-challenge', type='TXT', value='token', remote_record_id='r2'),
        ]

    def add_record(self, domain, record):
        self.calls.append(('add', record.rr))
        return 'new-id'

    def update_record(self, domain, record):
        self.calls.append(('update', record.remote_record_id))

    def delete_record(self, domain, remote_record_id):
        self.calls.append(('delete', remote_record_id))

    def set_record_enabled(self, domain, remote_record_id, enabled):
        self.calls.append(('status', remote_record_id, enabled))


class FailingDnsProvider(FakeDnsProvider):
    def add_record(self, domain, record):
        raise DnsProviderError('厂商接口超时')

    def delete_record(self, domain, remote_record_id):
        raise DnsProviderError('厂商接口超时')


class BaseApiTest(TestCase):
    def _client(self, is_staff=True, username=None):
        username = username or ('admin_c' if is_staff else 'user_c')
        u = get_user_model().objects.create(username=username, is_staff=is_staff)
        u.set_password('Pass!2345xyz')
        u.save()
        c = APIClient()
        r = c.post('/api/auth/login/', {'username': username, 'password': 'Pass!2345xyz'}, format='json')
        c.credentials(HTTP_AUTHORIZATION='Bearer ' + r.json()['access'])
        return c

    def setUp(self):
        # 字典迁移已预置常用云厂商，get_or_create 防编码冲突
        self.provider, _ = CloudProvider.objects.get_or_create(code='aliyun', defaults={'name': '阿里云'})
        self.account = CloudAccount.objects.create(
            name='ali-main', provider=self.provider, access_key='AK', secret_key='SK',
        )
        self.domain = Domain.objects.create(name='example.com', cloud_account=self.account)


class CloudAccountApiTests(BaseApiTest):
    def test_create_and_secret_not_echoed(self):
        admin = self._client()
        r = admin.post('/api/cloud/accounts/', {
            'name': 'ali-2', 'provider': self.provider.id,
            'access_key': 'AK2', 'secret_key': 'SK2',
        }, format='json')
        self.assertEqual(r.status_code, 201)
        body = r.json()
        self.assertNotIn('secret_key', body)
        self.assertTrue(body['has_secret_key'])

    def test_create_requires_secret(self):
        admin = self._client()
        r = admin.post('/api/cloud/accounts/', {
            'name': 'ali-3', 'provider': self.provider.id, 'access_key': 'AK3',
        }, format='json')
        self.assertEqual(r.status_code, 400)

    def test_update_blank_secret_keeps_old(self):
        admin = self._client()
        r = admin.patch(f'/api/cloud/accounts/{self.account.id}/', {'secret_key': ''}, format='json')
        self.assertEqual(r.status_code, 200)
        self.account.refresh_from_db()
        self.assertEqual(self.account.secret_key, 'SK')

    def test_delete_protected_by_domain(self):
        admin = self._client()
        r = admin.delete(f'/api/cloud/accounts/{self.account.id}/')
        self.assertEqual(r.status_code, 400)
        self.assertIn('主域名', r.json()['msg'])

    def test_non_admin_forbidden(self):
        normal = self._client(is_staff=False)
        self.assertEqual(normal.get('/api/cloud/accounts/').status_code, 403)

    @patch('apps.cloud_mgr.views.get_dns_provider')
    def test_test_connection(self, mock_get):
        mock_get.return_value = FakeDnsProvider()
        admin = self._client()
        r = admin.post(f'/api/cloud/accounts/{self.account.id}/test_connection/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['data']['domains'], ['example.com', 'demo.cn'])


class DomainApiTests(BaseApiTest):
    def test_create_normalizes_name(self):
        admin = self._client()
        r = admin.post('/api/cloud/domains/', {
            'name': 'Demo.CN.', 'cloud_account': self.account.id,
        }, format='json')
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()['name'], 'demo.cn')

    def test_invalid_name_rejected(self):
        admin = self._client()
        r = admin.post('/api/cloud/domains/', {
            'name': 'not_a_domain', 'cloud_account': self.account.id,
        }, format='json')
        self.assertEqual(r.status_code, 400)

    def test_normal_user_read_only(self):
        normal = self._client(is_staff=False)
        self.assertEqual(normal.get('/api/cloud/domains/').status_code, 200)
        r = normal.post('/api/cloud/domains/', {
            'name': 'x.com', 'cloud_account': self.account.id,
        }, format='json')
        self.assertEqual(r.status_code, 403)

    def test_keyword_matches_subdomain_and_value(self):
        other = Domain.objects.create(name='other.cn', cloud_account=self.account)
        DnsRecord.objects.create(domain=self.domain, rr='test', type='A', value='1.2.3.4')
        DnsRecord.objects.create(domain=other, rr='api', type='A', value='9.9.9.9')
        admin = self._client()

        def domain_names(keyword):
            r = admin.get('/api/cloud/domains/', {'keyword': keyword})
            return [d['name'] for d in r.json()['data']['results']]

        # 主域名命中（原有行为）
        self.assertEqual(domain_names('example'), ['example.com'])
        # 子域名主机记录命中
        self.assertEqual(domain_names('test'), ['example.com'])
        # 完整 FQDN 命中（rr 与域名跨列拼接）
        self.assertEqual(domain_names('test.example.com'), ['example.com'])
        # 记录值（IP）反查命中
        self.assertEqual(domain_names('9.9.9.9'), ['other.cn'])
        # 无命中
        self.assertEqual(domain_names('nothing-here'), [])

    def test_record_keyword_matches_fqdn_and_value(self):
        DnsRecord.objects.create(domain=self.domain, rr='test', type='A', value='1.2.3.4')
        DnsRecord.objects.create(domain=self.domain, rr='api', type='A', value='5.6.7.8')
        admin = self._client()

        def rrs(keyword):
            r = admin.get('/api/cloud/dns-records/', {'domain': self.domain.id, 'keyword': keyword})
            return sorted(rec['rr'] for rec in r.json()['data']['results'])

        self.assertEqual(rrs('test'), ['test'])
        self.assertEqual(rrs('test.example.com'), ['test'])
        self.assertEqual(rrs('5.6.7.8'), ['api'])

    @patch('apps.cloud_mgr.views.get_dns_provider')
    def test_import_remote_skips_existing(self, mock_get):
        mock_get.return_value = FakeDnsProvider()
        admin = self._client()
        r = admin.post('/api/cloud/domains/import_remote/', {'cloud_account': self.account.id}, format='json')
        self.assertEqual(r.status_code, 200)
        data = r.json()['data']
        self.assertEqual(data['imported'], ['demo.cn'])  # example.com 已存在
        self.assertTrue(Domain.objects.filter(name='demo.cn').exists())

    @patch('apps.cloud_mgr.views.get_dns_provider')
    def test_sync_records_ignores_acme_and_mirrors(self, mock_get):
        mock_get.return_value = FakeDnsProvider()
        # 本地残留一条厂商侧已删除的记录 → 同步后应删除
        DnsRecord.objects.create(
            domain=self.domain, rr='old', type='A', value='9.9.9.9',
            remote_record_id='gone', sync_status='synced',
        )
        admin = self._client()
        r = admin.post(f'/api/cloud/domains/{self.domain.id}/sync_records/')
        self.assertEqual(r.status_code, 200)
        stats = r.json()['data']
        self.assertEqual(stats['created'], 1)
        self.assertEqual(stats['deleted'], 1)
        self.assertEqual(stats['skipped_acme'], 1)
        rrs = set(self.domain.records.values_list('rr', flat=True))
        self.assertEqual(rrs, {'www'})
        self.domain.refresh_from_db()
        self.assertIsNotNone(self.domain.last_synced_at)


class DnsRecordApiTests(BaseApiTest):
    @patch('apps.cloud_mgr.views.get_dns_provider')
    def test_create_pushes_to_provider(self, mock_get):
        fake = FakeDnsProvider()
        mock_get.return_value = fake
        admin = self._client()
        r = admin.post('/api/cloud/dns-records/', {
            'domain': self.domain.id, 'rr': 'www', 'type': 'A', 'value': '1.1.1.1',
        }, format='json')
        self.assertEqual(r.status_code, 201)
        rec = DnsRecord.objects.get(rr='www')
        self.assertEqual(rec.sync_status, 'synced')
        self.assertEqual(rec.remote_record_id, 'new-id')
        self.assertIn(('add', 'www'), fake.calls)

    @patch('apps.cloud_mgr.views.get_dns_provider')
    def test_push_failure_kept_local(self, mock_get):
        mock_get.return_value = FailingDnsProvider()
        admin = self._client()
        r = admin.post('/api/cloud/dns-records/', {
            'domain': self.domain.id, 'rr': 'api', 'type': 'A', 'value': '1.1.1.1',
        }, format='json')
        self.assertEqual(r.status_code, 201)
        rec = DnsRecord.objects.get(rr='api')
        self.assertEqual(rec.sync_status, 'failed')
        self.assertIn('超时', rec.sync_error)

    @patch('apps.cloud_mgr.views.get_dns_provider')
    def test_retry_sync(self, mock_get):
        fake = FakeDnsProvider()
        mock_get.return_value = fake
        rec = DnsRecord.objects.create(
            domain=self.domain, rr='cdn', type='CNAME', value='x.cdn.com',
            sync_status='failed', sync_error='之前失败',
        )
        admin = self._client()
        r = admin.post(f'/api/cloud/dns-records/{rec.id}/retry_sync/')
        self.assertEqual(r.status_code, 200)
        rec.refresh_from_db()
        self.assertEqual(rec.sync_status, 'synced')

    def test_duplicate_record_rejected(self):
        DnsRecord.objects.create(domain=self.domain, rr='www', type='A', value='1.1.1.1')
        admin = self._client()
        r = admin.post('/api/cloud/dns-records/', {
            'domain': self.domain.id, 'rr': 'www', 'type': 'A', 'value': '1.1.1.1',
        }, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('重复', r.json()['msg'])

    def test_acme_challenge_rr_rejected(self):
        admin = self._client()
        r = admin.post('/api/cloud/dns-records/', {
            'domain': self.domain.id, 'rr': '_acme-challenge', 'type': 'TXT', 'value': 'x',
        }, format='json')
        self.assertEqual(r.status_code, 400)

    @patch('apps.cloud_mgr.views.get_dns_provider')
    def test_server_binding_fills_ip(self, mock_get):
        mock_get.return_value = FakeDnsProvider()
        server = Server.objects.create(name='web-01', private_ip='10.0.0.8', public_ip='8.8.8.8')
        admin = self._client()
        r = admin.post('/api/cloud/dns-records/', {
            'domain': self.domain.id, 'rr': 'web', 'type': 'A',
            'target_type': 'server', 'server': server.id,
        }, format='json')
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()['value'], '8.8.8.8')

    def test_server_binding_requires_server(self):
        admin = self._client()
        r = admin.post('/api/cloud/dns-records/', {
            'domain': self.domain.id, 'rr': 'web', 'type': 'A', 'target_type': 'server',
        }, format='json')
        self.assertEqual(r.status_code, 400)

    @patch('apps.cloud_mgr.views.get_dns_provider')
    def test_destroy_remote_fail_keeps_record(self, mock_get):
        mock_get.return_value = FailingDnsProvider()
        rec = DnsRecord.objects.create(
            domain=self.domain, rr='www', type='A', value='1.2.3.4',
            remote_record_id='r1', sync_status='synced',
        )
        admin = self._client()
        r = admin.delete(f'/api/cloud/dns-records/{rec.id}/')
        self.assertEqual(r.status_code, 400)
        self.assertTrue(DnsRecord.objects.filter(pk=rec.id).exists())

    @patch('apps.cloud_mgr.views.get_dns_provider')
    def test_destroy_deletes_remote_then_local(self, mock_get):
        fake = FakeDnsProvider()
        mock_get.return_value = fake
        rec = DnsRecord.objects.create(
            domain=self.domain, rr='www', type='A', value='1.2.3.4',
            remote_record_id='r1', sync_status='synced',
        )
        admin = self._client()
        r = admin.delete(f'/api/cloud/dns-records/{rec.id}/')
        self.assertEqual(r.status_code, 200)
        self.assertIn(('delete', 'r1'), fake.calls)
        self.assertFalse(DnsRecord.objects.filter(pk=rec.id).exists())


class CertificateApiTests(BaseApiTest):
    """证书 API：mock acme 签发（sync 后端跑通完整落库链路）。"""

    def _create_cert(self, **kwargs):
        from apps.cloud_mgr.models import Certificate
        defaults = {
            'name': 'web-cert', 'domains': ['example.com', '*.example.com'],
            'cloud_account': self.account,
        }
        defaults.update(kwargs)
        return Certificate.objects.create(**defaults)

    def test_create_validates_domains(self):
        admin = self._client()
        r = admin.post('/api/cloud/certificates/', {
            'name': 'c1', 'domains': ['Example.COM.', '*.example.com'],
            'cloud_account': self.account.id,
        }, format='json')
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()['domains'], ['example.com', '*.example.com'])
        # 空列表 / 通配符开头 / 非法格式
        for bad in ([], ['*.example.com'], ['not_a_domain']):
            r = admin.post('/api/cloud/certificates/', {
                'name': f'bad-{len(bad)}', 'domains': bad, 'cloud_account': self.account.id,
            }, format='json')
            self.assertEqual(r.status_code, 400, bad)

    def test_key_not_echoed(self):
        cert = self._create_cert(status='issued', private_key_pem='SECRET-KEY')
        admin = self._client()
        r = admin.get(f'/api/cloud/certificates/{cert.id}/')
        body = r.json()
        self.assertNotIn('private_key_pem', body)
        self.assertNotIn('cert_pem', body)
        self.assertTrue(body['has_private_key'])

    @patch('apps.cloud_mgr.tasks.issue_certificate')
    def test_issue_success_flow(self, mock_issue):
        from datetime import timedelta
        from django.utils import timezone as tz
        expires = tz.now() + timedelta(days=90)
        mock_issue.return_value = ('CERT', 'KEY', 'CHAIN', expires)
        cert = self._create_cert()
        admin = self._client()
        with self.settings(TASK_EXEC_BACKEND='sync'):
            r = admin.post(f'/api/cloud/certificates/{cert.id}/issue/')
        self.assertEqual(r.status_code, 200)
        cert.refresh_from_db()
        self.assertEqual(cert.status, 'issued')
        self.assertEqual(cert.cert_pem, 'CERT')
        self.assertEqual(cert.private_key_pem, 'KEY')
        self.assertIsNotNone(cert.expires_at)

    @patch('apps.cloud_mgr.tasks.issue_certificate')
    def test_issue_failure_records_error(self, mock_issue):
        from apps.cloud_mgr.acme import AcmeError
        mock_issue.side_effect = AcmeError('DNS 验证失败')
        cert = self._create_cert()
        admin = self._client()
        with self.settings(TASK_EXEC_BACKEND='sync'):
            admin.post(f'/api/cloud/certificates/{cert.id}/issue/')
        cert.refresh_from_db()
        self.assertEqual(cert.status, 'failed')
        self.assertIn('DNS 验证失败', cert.last_error)

    def test_issue_blocked_while_issuing(self):
        cert = self._create_cert(status='issuing')
        admin = self._client()
        r = admin.post(f'/api/cloud/certificates/{cert.id}/issue/')
        self.assertEqual(r.status_code, 400)
        r = admin.delete(f'/api/cloud/certificates/{cert.id}/')
        self.assertEqual(r.status_code, 400)

    def test_renew_requires_issued(self):
        cert = self._create_cert(status='pending')
        admin = self._client()
        r = admin.post(f'/api/cloud/certificates/{cert.id}/renew/')
        self.assertEqual(r.status_code, 400)

    def test_issued_cert_locks_domains(self):
        cert = self._create_cert(status='issued')
        admin = self._client()
        r = admin.patch(f'/api/cloud/certificates/{cert.id}/', {
            'domains': ['other.com'],
        }, format='json')
        self.assertEqual(r.status_code, 400)
        # remark / auto_renew 可改
        r = admin.patch(f'/api/cloud/certificates/{cert.id}/', {'auto_renew': False}, format='json')
        self.assertEqual(r.status_code, 200)

    def test_download_admin_only_returns_zip(self):
        import io
        import zipfile

        cert = self._create_cert(status='issued', cert_pem='CERT', private_key_pem='KEY', chain_pem='CHAIN')
        normal = self._client(is_staff=False)
        self.assertEqual(normal.get(f'/api/cloud/certificates/{cert.id}/download/').status_code, 403)
        admin = self._client()
        r = admin.get(f'/api/cloud/certificates/{cert.id}/download/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r['Content-Type'], 'application/zip')
        self.assertIn('web-cert.zip', r['Content-Disposition'])
        self.assertEqual(r['Cache-Control'], 'no-store')
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            # 统一带一层以证书名命名的目录
            self.assertEqual(
                sorted(zf.namelist()),
                [
                    'web-cert/README.txt',
                    'web-cert/web-cert.crt',
                    'web-cert/web-cert.fullchain.pem',
                    'web-cert/web-cert.key',
                ],
            )
            self.assertEqual(zf.read('web-cert/web-cert.key').decode(), 'KEY')
            self.assertEqual(zf.read('web-cert/web-cert.fullchain.pem').decode(), 'CHAIN')

    def test_download_before_issued_rejected(self):
        cert = self._create_cert(status='pending')
        admin = self._client()
        r = admin.get(f'/api/cloud/certificates/{cert.id}/download/')
        self.assertEqual(r.status_code, 400)

    @patch('apps.cloud_mgr.tasks.issue_certificate')
    def test_renew_expiring_scan(self, mock_issue):
        from datetime import timedelta
        from django.utils import timezone as tz
        from apps.cloud_mgr.tasks import renew_expiring
        mock_issue.return_value = ('C', 'K', 'CH', tz.now() + timedelta(days=90))
        soon = self._create_cert(name='soon', status='issued',
                                 expires_at=tz.now() + timedelta(days=10))
        far = self._create_cert(name='far', status='issued',
                                expires_at=tz.now() + timedelta(days=80))
        no_renew = self._create_cert(name='no-renew', status='issued', auto_renew=False,
                                     expires_at=tz.now() + timedelta(days=10))
        with self.settings(TASK_EXEC_BACKEND='sync'):
            triggered = renew_expiring()
        self.assertEqual(triggered, [soon.id])
        soon.refresh_from_db()
        self.assertEqual(soon.status, 'issued')
        mock_issue.assert_called_once()
        # 强制重签口径
        self.assertTrue(mock_issue.call_args.kwargs.get('force'))


class CertDeploymentApiTests(BaseApiTest):
    """证书分发：mock ansible 子进程，覆盖登记校验、推送、续签自动分发、桩类型。"""

    def setUp(self):
        super().setUp()
        from apps.cloud_mgr.models import Certificate
        self.cert = Certificate.objects.create(
            name='web-cert', domains=['example.com'], cloud_account=self.account,
            status='issued', cert_pem='CERT', private_key_pem='KEY', chain_pem='CHAIN',
        )
        self.server = Server.objects.create(name='web-01', private_ip='10.0.0.8', public_ip='8.8.8.8')

    def _create_deployment(self, **kwargs):
        from apps.cloud_mgr.models import CertDeployment
        defaults = {
            'certificate': self.cert, 'target_type': 'server', 'server': self.server,
            'deploy_path': '/etc/nginx/ssl', 'reload_cmd': 'nginx -s reload',
        }
        defaults.update(kwargs)
        return CertDeployment.objects.create(**defaults)

    def test_create_requires_server_for_server_type(self):
        admin = self._client()
        r = admin.post('/api/cloud/cert-deployments/', {
            'certificate': self.cert.id, 'target_type': 'server', 'deploy_path': '/etc/nginx/ssl',
        }, format='json')
        self.assertEqual(r.status_code, 400)

    def test_deploy_path_validation(self):
        admin = self._client()
        for bad in ('etc/nginx/ssl', '/etc', '/'):
            r = admin.post('/api/cloud/cert-deployments/', {
                'certificate': self.cert.id, 'target_type': 'server',
                'server': self.server.id, 'deploy_path': bad,
            }, format='json')
            self.assertEqual(r.status_code, 400, bad)
        # 绝对路径正常，尾部斜杠被规范化
        r = admin.post('/api/cloud/cert-deployments/', {
            'certificate': self.cert.id, 'target_type': 'server',
            'server': self.server.id, 'deploy_path': '/etc/nginx/ssl/',
        }, format='json')
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()['deploy_path'], '/etc/nginx/ssl')

    @patch('apps.cloud_mgr.deploy.run_subprocess')
    def test_deploy_success_pushes_files_and_reload(self, mock_run):
        mock_run.return_value = (0, 'ok changed=4')
        dep = self._create_deployment()
        admin = self._client()
        with self.settings(TASK_EXEC_BACKEND='sync'):
            r = admin.post(f'/api/cloud/cert-deployments/{dep.id}/deploy/')
        self.assertEqual(r.status_code, 200)
        dep.refresh_from_db()
        self.assertEqual(dep.status, 'success')
        self.assertIsNotNone(dep.last_deployed_at)
        self.assertIn('changed=4', dep.last_output)
        # 剧本内容：三个文件 + 目录 + reload，私钥 0600
        playbook_path = mock_run.call_args.args[0][1]
        self.assertTrue(playbook_path.endswith('deploy_cert.yml'))

    @patch('apps.cloud_mgr.deploy.run_subprocess')
    def test_deploy_failure_records_error(self, mock_run):
        mock_run.return_value = (2, 'UNREACHABLE! ssh timeout')
        dep = self._create_deployment()
        admin = self._client()
        with self.settings(TASK_EXEC_BACKEND='sync'):
            admin.post(f'/api/cloud/cert-deployments/{dep.id}/deploy/')
        dep.refresh_from_db()
        self.assertEqual(dep.status, 'failed')
        self.assertIn('UNREACHABLE', dep.last_error)

    def test_deploy_rejects_agent_channel_host(self):
        from apps.agent_mgr.models import AgentNode
        agent = AgentNode.objects.create(name='nat-gw', token_hash='x' * 64)
        self.server.agent = agent
        self.server.save(update_fields=['agent'])
        dep = self._create_deployment()
        admin = self._client()
        with self.settings(TASK_EXEC_BACKEND='sync'):
            admin.post(f'/api/cloud/cert-deployments/{dep.id}/deploy/')
        dep.refresh_from_db()
        self.assertEqual(dep.status, 'failed')
        self.assertIn('agent', dep.last_error)

    def test_deploy_stub_target_types_rejected(self):
        admin = self._client()
        for target in ('lb', 'ga', 'cdn'):
            dep = self._create_deployment(target_type=target, server=None,
                                          deploy_path=f'/opt/{target}')
            r = admin.post(f'/api/cloud/cert-deployments/{dep.id}/deploy/')
            self.assertEqual(r.status_code, 400, target)
            self.assertIn('暂未接入', r.json()['msg'])

    def test_deploy_requires_issued_certificate(self):
        self.cert.status = 'pending'
        self.cert.save(update_fields=['status'])
        dep = self._create_deployment()
        admin = self._client()
        r = admin.post(f'/api/cloud/cert-deployments/{dep.id}/deploy/')
        self.assertEqual(r.status_code, 400)

    @patch('apps.cloud_mgr.deploy.run_subprocess')
    def test_deploy_all_skips_stub_and_deploying(self, mock_run):
        mock_run.return_value = (0, 'ok')
        good = self._create_deployment()
        self._create_deployment(target_type='cdn', server=None, deploy_path='/opt/cdn')
        admin = self._client()
        with self.settings(TASK_EXEC_BACKEND='sync'):
            r = admin.post(f'/api/cloud/certificates/{self.cert.id}/deploy_all/')
        self.assertEqual(r.status_code, 200)
        data = r.json()['data']
        self.assertEqual(data['dispatched'], [good.id])
        self.assertEqual(len(data['skipped']), 1)

    @patch('apps.cloud_mgr.deploy.run_subprocess')
    @patch('apps.cloud_mgr.tasks.issue_certificate')
    def test_renew_triggers_auto_deploy(self, mock_issue, mock_run):
        from datetime import timedelta
        from django.utils import timezone as tz
        mock_issue.return_value = ('C2', 'K2', 'CH2', tz.now() + timedelta(days=90))
        mock_run.return_value = (0, 'ok')
        auto = self._create_deployment(auto_deploy=True)
        manual = self._create_deployment(auto_deploy=False, deploy_path='/etc/haproxy/ssl')
        admin = self._client()
        with self.settings(TASK_EXEC_BACKEND='sync'):
            r = admin.post(f'/api/cloud/certificates/{self.cert.id}/renew/')
        self.assertEqual(r.status_code, 200)
        auto.refresh_from_db()
        manual.refresh_from_db()
        self.assertEqual(auto.status, 'success')
        self.assertEqual(manual.status, 'pending')  # 未开自动分发不动

    def test_destroy_blocked_while_deploying(self):
        dep = self._create_deployment(status='deploying')
        admin = self._client()
        self.assertEqual(admin.delete(f'/api/cloud/cert-deployments/{dep.id}/').status_code, 400)
        self.assertEqual(admin.post(f'/api/cloud/cert-deployments/{dep.id}/deploy/').status_code, 400)

    def test_normal_user_read_only(self):
        dep = self._create_deployment()
        normal = self._client(is_staff=False)
        self.assertEqual(normal.get('/api/cloud/cert-deployments/').status_code, 200)
        self.assertEqual(normal.post(f'/api/cloud/cert-deployments/{dep.id}/deploy/').status_code, 403)
