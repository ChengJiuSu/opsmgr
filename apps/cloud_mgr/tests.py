"""cloud_mgr 接口测试：云账号/主域名/解析记录 CRUD、推送同步（mock 厂商）、权限。"""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from common.testing import grant_sudo

from apps.asset_mgr.models import CloudProvider, Server
from apps.cloud_mgr.models import (
    CloudAccount,
    DnsChangeLog,
    DnsChangeRequest,
    DnsRecord,
    Domain,
)
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


class FailingListProvider(FakeDnsProvider):
    """拉列表失败：留痕取不到云端前值时的退路（退回本地值 + 标注来源）。"""

    def list_records(self, domain):
        raise DnsProviderError('厂商接口超时')


class FailingUpdateProvider(FakeDnsProvider):
    """更新失败：回滚时云端先动，失败必须整体放弃。"""

    def update_record(self, domain, record):
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
        # 云账号写操作要求 sudo 二次验证（common/sudo.py）
        if is_staff:
            grant_sudo(c)
        return c

    def _client_without_sudo(self, username):
        """管理员但未开 sudo 窗口：验「写操作要二次验证」用。"""
        user = get_user_model().objects.create(username=username, is_staff=True)
        user.set_password('Pass!2345xyz')
        user.save()
        c = APIClient()
        r = c.post('/api/auth/login/',
                   {'username': username, 'password': 'Pass!2345xyz'}, format='json')
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

    def test_keyword_matches_name_and_access_key(self):
        CloudAccount.objects.create(
            name='tx-backup', provider=self.provider, access_key='AKID-TX', secret_key='S',
        )
        admin = self._client()

        def names(keyword):
            r = admin.get('/api/cloud/accounts/', {'keyword': keyword})
            return sorted(a['name'] for a in r.json()['data']['results'])

        self.assertEqual(names('backup'), ['tx-backup'])
        self.assertEqual(names('AKID'), ['tx-backup'])
        self.assertEqual(names('ali'), ['ali-main'])

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


@override_settings(DNS_APPROVAL_ENABLED=False)
class DnsRecordApiTests(BaseApiTest):
    """解析记录推送机制。关掉审批直测执行链路——审批策略由 DnsApprovalTests 覆盖。"""

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

    def test_write_requires_sudo(self):
        """reload_cmd 是目标主机上执行的任意命令，登记它必须过二次验证。"""
        admin = self._client(username='nosudo_admin')
        admin.post('/api/auth/logout/')  # 作废会话，下面重新登录且不开 sudo 窗口
        fresh = APIClient()
        r = fresh.post('/api/auth/login/',
                       {'username': 'nosudo_admin', 'password': 'Pass!2345xyz'}, format='json')
        fresh.credentials(HTTP_AUTHORIZATION='Bearer ' + r.json()['access'])
        r = fresh.post('/api/cloud/cert-deployments/', {
            'certificate': self.cert.id, 'target_type': 'server', 'server': self.server.id,
            'deploy_path': '/etc/nginx/ssl', 'reload_cmd': 'rm -rf /',
        }, format='json')
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()['error_code'], 'sudo_required')

    def test_deploy_action_does_not_require_sudo(self):
        """分发执行的是已登记配置，需要复核的是配置本身，不该每次都弹验证框。"""
        dep = self._create_deployment()
        self._client(username='deploy_admin')
        fresh = APIClient()
        r = fresh.post('/api/auth/login/',
                       {'username': 'deploy_admin', 'password': 'Pass!2345xyz'}, format='json')
        fresh.credentials(HTTP_AUTHORIZATION='Bearer ' + r.json()['access'])
        with patch('apps.cloud_mgr.deploy.run_subprocess', return_value=(0, 'ok')), \
                self.settings(TASK_EXEC_BACKEND='sync'):
            r = fresh.post(f'/api/cloud/cert-deployments/{dep.id}/deploy/')
        self.assertEqual(r.status_code, 200, r.content)

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
        self.server.channel_mode = 'agent'
        self.server.agent = agent
        self.server.save(update_fields=['channel_mode', 'agent'])
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


class CloudSecretEncryptionTests(TestCase):
    """云账号 AccessKey Secret 与证书私钥加密存储：库里密文，ORM 明文。"""

    SECRET = 'ak-secret-value-0001'
    KEY_PEM = '-----BEGIN PRIVATE KEY-----\nzzz\n-----END PRIVATE KEY-----'

    def setUp(self):
        self.provider, _ = CloudProvider.objects.get_or_create(
            code='aliyun', defaults={'name': '阿里云'})

    @staticmethod
    def _raw(table, column, pk):
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute(f'SELECT {column} FROM {table} WHERE id = %s', [pk])
            return cursor.fetchone()[0]

    def test_account_secret_encrypted(self):
        account = CloudAccount.objects.create(
            name='enc-account', provider=self.provider,
            access_key='AK123', secret_key=self.SECRET)
        stored = self._raw('cloud_account', 'secret_key', account.pk)
        self.assertTrue(stored.startswith('gAAAAA'))
        self.assertNotIn(self.SECRET, stored)
        # provider 调厂商 API 要用原值，所以 ORM 必须读回明文
        self.assertEqual(CloudAccount.objects.get(pk=account.pk).secret_key, self.SECRET)

    def test_certificate_private_key_encrypted(self):
        from apps.cloud_mgr.models import Certificate

        account = CloudAccount.objects.create(
            name='enc-account-2', provider=self.provider,
            access_key='AK123', secret_key=self.SECRET)
        cert = Certificate.objects.create(
            name='enc-cert', domains=['a.example.com'], cloud_account=account,
            status='issued', cert_pem='CERT', private_key_pem=self.KEY_PEM)
        stored = self._raw('cloud_certificate', 'private_key_pem', cert.pk)
        self.assertTrue(stored.startswith('gAAAAA'))
        self.assertNotIn('BEGIN PRIVATE KEY', stored)
        # 分发到主机与打包下载都要原值
        self.assertEqual(Certificate.objects.get(pk=cert.pk).private_key_pem, self.KEY_PEM)


@override_settings(DNS_APPROVAL_ENABLED=False)
class DnsChangeLogTests(BaseApiTest):
    """DNS 变更留痕与回滚：前值来源、改删留痕、一键还原、闸门。

    关掉审批：这里测的是「改完之后能不能查、能不能撤」，走审批路径会把每个用例
    都变成两步。审批路径同样落留痕，由 DnsApprovalTests 断言。
    """

    def setUp(self):
        super().setUp()
        self.record = DnsRecord.objects.create(
            domain=self.domain, rr='www', type='A', value='1.2.3.4',
            ttl=600, line='default', remote_record_id='r1', sync_status='synced',
        )

    @staticmethod
    def _patches(provider):
        """views 与 dns_history 各自导入了 get_dns_provider，两处都要打。"""
        return (
            patch('apps.cloud_mgr.views.get_dns_provider', return_value=provider),
            patch('apps.cloud_mgr.dns_history.get_dns_provider', return_value=provider),
        )

    def _with_provider(self, provider, func):
        p1, p2 = self._patches(provider)
        with p1, p2:
            return func()

    # ── 留痕 ──────────────────────────────────────────────
    def test_update_logs_provider_before_value(self):
        """前值取自厂商实时值：本地与云端可能不一致（sync_status=failed 就是）。"""
        admin = self._client()
        fake = FakeDnsProvider()
        r = self._with_provider(fake, lambda: admin.patch(
            f'/api/cloud/dns-records/{self.record.id}/',
            {'value': '9.9.9.9', 'reason': '切流量到新机'}, format='json'))
        self.assertEqual(r.status_code, 200, r.content)

        log = DnsChangeLog.objects.get(action='update')
        self.assertEqual(log.before_source, 'provider')
        self.assertEqual(log.before['value'], '1.2.3.4')  # FakeDnsProvider 的 r1
        self.assertEqual(log.after['value'], '9.9.9.9')
        self.assertEqual(log.reason, '切流量到新机')
        self.assertEqual(log.operator.username, 'admin_c')
        self.assertTrue(log.restorable)

    def test_delete_logs_before_value(self):
        """核心用例：删完本地行没了、DELETE 请求又没 body，只有这张表能回答删了什么。"""
        admin = self._client()
        fake = FakeDnsProvider()
        r = self._with_provider(fake, lambda: admin.delete(
            f'/api/cloud/dns-records/{self.record.id}/'))
        self.assertEqual(r.status_code, 200, r.content)
        self.assertFalse(DnsRecord.objects.filter(pk=self.record.pk).exists())

        log = DnsChangeLog.objects.get(action='delete')
        self.assertEqual(log.record_rr, 'www')
        self.assertEqual(log.domain_name, self.domain.name)
        self.assertEqual(log.before['value'], '1.2.3.4')
        self.assertTrue(log.restorable)

    def test_create_logs_after_only(self):
        admin = self._client()
        fake = FakeDnsProvider()
        self._with_provider(fake, lambda: admin.post('/api/cloud/dns-records/', {
            'domain': self.domain.id, 'rr': 'new', 'type': 'A', 'value': '5.5.5.5',
        }, format='json'))
        log = DnsChangeLog.objects.get(action='create')
        self.assertEqual(log.before, {})
        self.assertEqual(log.before_source, 'none')
        self.assertEqual(log.after['value'], '5.5.5.5')

    def test_provider_failure_falls_back_to_local_and_blocks_restore(self):
        """拉不到云端前值时退回本地并标注 —— 且这种快照不允许回滚。"""
        admin = self._client()
        r = self._with_provider(FailingListProvider(), lambda: admin.patch(
            f'/api/cloud/dns-records/{self.record.id}/', {'value': '8.8.8.8'}, format='json'))
        self.assertEqual(r.status_code, 200, r.content)
        log = DnsChangeLog.objects.get(action='update')
        self.assertEqual(log.before_source, 'local')
        self.assertFalse(log.restorable)

    # ── 回滚 ──────────────────────────────────────────────
    def test_restore_update(self):
        admin = self._client()
        fake = FakeDnsProvider()
        self._with_provider(fake, lambda: admin.patch(
            f'/api/cloud/dns-records/{self.record.id}/', {'value': '9.9.9.9'}, format='json'))
        log = DnsChangeLog.objects.get(action='update')

        r = self._with_provider(fake, lambda: admin.post(
            f'/api/cloud/dns-changes/{log.id}/restore/'))
        self.assertEqual(r.status_code, 200, r.content)
        self.record.refresh_from_db()
        self.assertEqual(self.record.value, '1.2.3.4')
        log.refresh_from_db()
        self.assertIsNotNone(log.restored_at)
        self.assertFalse(log.restorable)  # 回滚过的不能再回滚
        # 回滚本身也留痕，否则「谁把它改回去的」又查不到
        self.assertTrue(DnsChangeLog.objects.filter(action='restore').exists())

    def test_restore_delete_recreates_record(self):
        admin = self._client()
        fake = FakeDnsProvider()
        self._with_provider(fake, lambda: admin.delete(
            f'/api/cloud/dns-records/{self.record.id}/'))
        log = DnsChangeLog.objects.get(action='delete')

        r = self._with_provider(fake, lambda: admin.post(
            f'/api/cloud/dns-changes/{log.id}/restore/'))
        self.assertEqual(r.status_code, 200, r.content)
        rebuilt = DnsRecord.objects.get(domain=self.domain, rr='www')
        self.assertEqual(rebuilt.value, '1.2.3.4')
        self.assertEqual(rebuilt.remote_record_id, 'new-id')  # 云端新增给的新 id
        self.assertEqual(rebuilt.sync_status, 'synced')

    def test_restore_create_deletes_record(self):
        admin = self._client()
        fake = FakeDnsProvider()
        self._with_provider(fake, lambda: admin.post('/api/cloud/dns-records/', {
            'domain': self.domain.id, 'rr': 'oops', 'type': 'A', 'value': '5.5.5.5',
        }, format='json'))
        log = DnsChangeLog.objects.get(action='create')

        r = self._with_provider(fake, lambda: admin.post(
            f'/api/cloud/dns-changes/{log.id}/restore/'))
        self.assertEqual(r.status_code, 200, r.content)
        self.assertFalse(DnsRecord.objects.filter(rr='oops').exists())
        self.assertIn(('delete', 'new-id'), fake.calls)

    def test_restore_provider_failure_changes_nothing(self):
        """云端先动、本地后动：云端失败不能留下「页面显示已回滚、线上还是错值」。"""
        admin = self._client()
        fake = FakeDnsProvider()
        self._with_provider(fake, lambda: admin.patch(
            f'/api/cloud/dns-records/{self.record.id}/', {'value': '9.9.9.9'}, format='json'))
        log = DnsChangeLog.objects.get(action='update')

        r = self._with_provider(FailingUpdateProvider(), lambda: admin.post(
            f'/api/cloud/dns-changes/{log.id}/restore/'))
        self.assertEqual(r.status_code, 400)
        self.record.refresh_from_db()
        self.assertEqual(self.record.value, '9.9.9.9')
        log.refresh_from_db()
        self.assertIsNone(log.restored_at)

    # ── 闸门 ──────────────────────────────────────────────
    def test_update_and_delete_require_sudo(self):
        fresh = self._client_without_sudo('dns_nosudo')
        fake = FakeDnsProvider()
        r = self._with_provider(fake, lambda: fresh.patch(
            f'/api/cloud/dns-records/{self.record.id}/', {'value': '7.7.7.7'}, format='json'))
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()['error_code'], 'sudo_required')

        r = self._with_provider(fake, lambda: fresh.delete(
            f'/api/cloud/dns-records/{self.record.id}/'))
        self.assertEqual(r.status_code, 403)

    def test_create_does_not_require_sudo(self):
        """加一条新子域名不会动到现有解析，不该被拦。"""
        fresh = self._client_without_sudo('dns_nosudo2')
        r = self._with_provider(FakeDnsProvider(), lambda: fresh.post(
            '/api/cloud/dns-records/', {
                'domain': self.domain.id, 'rr': 'fresh', 'type': 'A', 'value': '6.6.6.6',
            }, format='json'))
        self.assertEqual(r.status_code, 201, r.content)

    def test_change_log_is_read_only(self):
        """能被操作人删掉的留痕等于没有留痕。"""
        admin = self._client()
        log = DnsChangeLog.objects.create(
            domain=self.domain, domain_name=self.domain.name, action='update',
            before={'value': 'x'}, before_source='provider')
        self.assertEqual(admin.delete(f'/api/cloud/dns-changes/{log.id}/').status_code, 405)

    def test_non_admin_read_only(self):
        member = self._client(is_staff=False, username='dns_member')
        self.assertEqual(member.get('/api/cloud/dns-changes/').status_code, 200)


class DriftDnsProvider(FakeDnsProvider):
    """云端值可变的假厂商：模拟「申请之后线上值被别人改了」。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.current_value = '1.2.3.4'

    def list_records(self, domain):
        return [DnsRecordData(rr='www', type='A', value=self.current_value,
                              ttl=600, remote_record_id='r1')]


class DnsApprovalTests(BaseApiTest):
    """DNS 高危变更审批：分级、建单、审批资格、紧急通道、过期、漂移比对。"""

    def setUp(self):
        super().setUp()
        self.record = DnsRecord.objects.create(
            domain=self.domain, rr='www', type='A', value='1.2.3.4',
            ttl=600, line='default', remote_record_id='r1', sync_status='synced',
        )

    def _with(self, provider, func):
        with patch('apps.cloud_mgr.views.get_dns_provider', return_value=provider), \
                patch('apps.cloud_mgr.dns_history.get_dns_provider', return_value=provider), \
                patch('apps.cloud_mgr.dns_approval.get_dns_provider', return_value=provider):
            return func()

    def _patch_record(self, client, payload, provider=None):
        return self._with(provider or FakeDnsProvider(), lambda: client.patch(
            f'/api/cloud/dns-records/{self.record.id}/', payload, format='json'))

    # ── 分级 ──────────────────────────────────────────────
    def test_low_risk_create_executes_directly(self):
        """加一条新的测试子域名不该被拦——全都拦会让人想办法躲开流程。"""
        admin = self._client()
        r = self._with(FakeDnsProvider(), lambda: admin.post('/api/cloud/dns-records/', {
            'domain': self.domain.id, 'rr': 'lowrisk', 'type': 'A', 'value': '9.9.9.9',
        }, format='json'))
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(DnsChangeRequest.objects.count(), 0)

    def test_high_risk_create_needs_approval(self):
        """根域 / NS / MX 即使是新增也要审批。"""
        admin = self._client()
        r = self._with(FakeDnsProvider(), lambda: admin.post('/api/cloud/dns-records/', {
            'domain': self.domain.id, 'rr': '@', 'type': 'A', 'value': '9.9.9.9',
            'reason': '上线主域名',
        }, format='json'))
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.json()['data']['pending_approval'])
        self.assertFalse(DnsRecord.objects.filter(rr='@').exists())
        cr = DnsChangeRequest.objects.get()
        self.assertIn('根域 @ 记录', cr.risk_reasons)

    def test_update_requires_reason(self):
        """高危必须填原因：审批人据此判断，事后排障也靠它。"""
        admin = self._client()
        r = self._patch_record(admin, {'value': '9.9.9.9'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('变更原因', r.json()['msg'])

    def test_update_creates_request_without_touching_record(self):
        admin = self._client()
        r = self._patch_record(admin, {'value': '9.9.9.9', 'reason': '切流量'})
        self.assertEqual(r.status_code, 200, r.content)
        self.record.refresh_from_db()
        self.assertEqual(self.record.value, '1.2.3.4')  # 未审批不落地
        cr = DnsChangeRequest.objects.get()
        self.assertEqual(cr.action, 'update')
        self.assertEqual(cr.payload['value'], '9.9.9.9')
        self.assertEqual(cr.before['value'], '1.2.3.4')
        self.assertEqual(cr.before_source, 'provider')
        self.assertEqual(cr.reason, '切流量')

    def test_delete_creates_request(self):
        admin = self._client()
        r = self._with(FakeDnsProvider(), lambda: admin.delete(
            f'/api/cloud/dns-records/{self.record.id}/',
            {'reason': '下线服务'}, format='json'))
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(DnsRecord.objects.filter(pk=self.record.pk).exists())
        self.assertEqual(DnsChangeRequest.objects.get().action, 'delete')

    def test_protected_flag_makes_low_risk_high(self):
        DnsRecord.objects.filter(pk=self.record.pk).update(remote_record_id='', protected=True)
        admin = self._client()
        r = self._patch_record(admin, {'value': '9.9.9.9', 'reason': 'x'})
        self.assertIn('记录被标记为受保护', DnsChangeRequest.objects.get().risk_reasons)
        self.assertEqual(r.status_code, 200)

    def test_sync_auto_protects_root_ns_mx(self):
        """等人工去标就会漏，所以 @ / NS / MX 同步时自动标。"""
        class RootProvider(FakeDnsProvider):
            def list_records(self, domain):
                return [
                    DnsRecordData(rr='@', type='A', value='1.1.1.1', remote_record_id='p1'),
                    DnsRecordData(rr='mail', type='MX', value='mx.x.com', remote_record_id='p2'),
                    DnsRecordData(rr='blog', type='A', value='2.2.2.2', remote_record_id='p3'),
                ]

        admin = self._client()
        self._with(RootProvider(), lambda: admin.post(
            f'/api/cloud/domains/{self.domain.id}/sync_records/'))
        self.assertTrue(DnsRecord.objects.get(rr='@').protected)
        self.assertTrue(DnsRecord.objects.get(rr='mail').protected)
        self.assertFalse(DnsRecord.objects.get(rr='blog').protected)

    # ── 审批资格 ──────────────────────────────────────────
    def _make_request(self, client=None):
        client = client or self._client()
        self._patch_record(client, {'value': '9.9.9.9', 'reason': '切流量'})
        return DnsChangeRequest.objects.get()

    def test_requester_cannot_self_approve(self):
        """10 人团队里最容易被绕过的就是这条，所以判定在后端而不是隐藏按钮。"""
        admin = self._client()
        cr = self._make_request(admin)
        r = self._with(FakeDnsProvider(), lambda: admin.post(
            f'/api/cloud/dns-change-requests/{cr.id}/approve/'))
        self.assertEqual(r.status_code, 400)
        self.assertIn('不能审批自己提交的变更单', r.json()['msg'])

    def test_other_admin_approves_and_applies(self):
        cr = self._make_request(self._client(username='requester_a'))
        reviewer = self._client(username='reviewer_a')
        r = self._with(FakeDnsProvider(), lambda: reviewer.post(
            f'/api/cloud/dns-change-requests/{cr.id}/approve/'))
        self.assertEqual(r.status_code, 200, r.content)
        self.record.refresh_from_db()
        self.assertEqual(self.record.value, '9.9.9.9')
        cr.refresh_from_db()
        self.assertEqual(cr.status, 'applied')
        self.assertEqual(cr.reviewed_by.username, 'reviewer_a')
        # 审批路径同样落留痕（回滚入口在那边）
        self.assertIsNotNone(cr.change_log)
        self.assertEqual(cr.change_log.before['value'], '1.2.3.4')

    def test_emergency_self_approve_requires_comment(self):
        """紧急通道必须留原因，否则它会变成日常绕过审批的后门。"""
        admin = self._client()
        cr = self._make_request(admin)
        r = self._with(FakeDnsProvider(), lambda: admin.post(
            f'/api/cloud/dns-change-requests/{cr.id}/approve/',
            {'emergency': True}, format='json'))
        self.assertEqual(r.status_code, 400)
        self.assertIn('必须填写原因', r.json()['msg'])

        r = self._with(FakeDnsProvider(), lambda: admin.post(
            f'/api/cloud/dns-change-requests/{cr.id}/approve/',
            {'emergency': True, 'comment': '线上故障，需立刻切流量'}, format='json'))
        self.assertEqual(r.status_code, 200, r.content)
        cr.refresh_from_db()
        self.assertTrue(cr.emergency)
        self.assertEqual(cr.status, 'applied')

    def test_approve_requires_admin_and_sudo(self):
        cr = self._make_request(self._client(username='requester_b'))
        member = self._client(is_staff=False, username='member_b')
        self.assertEqual(
            member.post(f'/api/cloud/dns-change-requests/{cr.id}/approve/').status_code, 403)

        nosudo = self._client_without_sudo('nosudo_reviewer')
        r = nosudo.post(f'/api/cloud/dns-change-requests/{cr.id}/approve/')
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()['error_code'], 'sudo_required')

    # ── 驳回 / 撤销 / 过期 / 漂移 ─────────────────────────
    def test_reject_keeps_record(self):
        cr = self._make_request(self._client(username='requester_c'))
        reviewer = self._client(username='reviewer_c')
        r = reviewer.post(f'/api/cloud/dns-change-requests/{cr.id}/reject/',
                          {'comment': '先别动'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        cr.refresh_from_db()
        self.assertEqual(cr.status, 'rejected')
        self.record.refresh_from_db()
        self.assertEqual(self.record.value, '1.2.3.4')

    def test_cancel_by_requester_only(self):
        requester = self._client(username='requester_d')
        cr = self._make_request(requester)
        member = self._client(is_staff=False, username='member_d')
        r = member.post(f'/api/cloud/dns-change-requests/{cr.id}/cancel/')
        self.assertEqual(r.status_code, 400)
        self.assertIn('只能撤销自己', r.json()['msg'])

        r = requester.post(f'/api/cloud/dns-change-requests/{cr.id}/cancel/')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(DnsChangeRequest.objects.get().status, 'cancelled')

    def test_expired_request_cannot_be_approved(self):
        """陈旧变更单被误批推上去是这类系统的典型事故。"""
        cr = self._make_request(self._client(username='requester_e'))
        DnsChangeRequest.objects.filter(pk=cr.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1))
        reviewer = self._client(username='reviewer_e')
        r = self._with(FakeDnsProvider(), lambda: reviewer.post(
            f'/api/cloud/dns-change-requests/{cr.id}/approve/'))
        self.assertEqual(r.status_code, 400)
        self.assertIn('已过期', r.json()['msg'])
        self.assertEqual(DnsChangeRequest.objects.get().status, 'expired')

    def test_drift_blocks_apply(self):
        """24 小时内线上值被别人改过：闭眼推陈旧目标值等于做了一次没人审过的变更。"""
        provider = DriftDnsProvider()
        requester = self._client(username='requester_f')
        self._patch_record(requester, {'value': '9.9.9.9', 'reason': '切流量'},
                           provider=provider)
        cr = DnsChangeRequest.objects.get()

        provider.current_value = '7.7.7.7'  # 别人改了线上值
        reviewer = self._client(username='reviewer_f')
        r = self._with(provider, lambda: reviewer.post(
            f'/api/cloud/dns-change-requests/{cr.id}/approve/'))
        self.assertEqual(r.status_code, 400)
        self.assertIn('已被改动', r.json()['msg'])
        cr.refresh_from_db()
        self.assertEqual(cr.status, 'pending')  # 保持待审批，供撤销后重提

    def test_delete_request_applied_removes_record(self):
        requester = self._client(username='requester_g')
        self._with(FakeDnsProvider(), lambda: requester.delete(
            f'/api/cloud/dns-records/{self.record.id}/',
            {'reason': '下线'}, format='json'))
        cr = DnsChangeRequest.objects.get()
        reviewer = self._client(username='reviewer_g')
        r = self._with(FakeDnsProvider(), lambda: reviewer.post(
            f'/api/cloud/dns-change-requests/{cr.id}/approve/'))
        self.assertEqual(r.status_code, 200, r.content)
        self.assertFalse(DnsRecord.objects.filter(pk=self.record.pk).exists())
        self.assertTrue(DnsChangeLog.objects.filter(action='delete').exists())

    def test_pending_filter_excludes_expired(self):
        cr = self._make_request(self._client(username='requester_h'))
        admin = self._client(username='lister')
        self.assertEqual(
            admin.get('/api/cloud/dns-change-requests/?status=pending').json()['data']['total'], 1)
        DnsChangeRequest.objects.filter(pk=cr.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(
            admin.get('/api/cloud/dns-change-requests/?status=pending').json()['data']['total'], 0)

    def test_risk_reasons_exposed_on_record(self):
        """前端据此在改之前就提示「本次变更需审批」。"""
        admin = self._client()
        data = admin.get(f'/api/cloud/dns-records/{self.record.id}/').json()
        self.assertIn('修改/删除已生效的线上解析', data['risk_reasons'])
