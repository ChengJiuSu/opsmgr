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
