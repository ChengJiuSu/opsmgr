"""审计测试：请求体脱敏（密码/验证码/私钥不得入库）与记录基本字段。"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from .middleware import DROPPED, NOT_RECORDED, REDACTED, redact_body
from .models import AuditLog

User = get_user_model()
PASSWORD = 'Pass!2345xyz'


class RedactBodyTests(TestCase):
    def test_empty(self):
        self.assertEqual(redact_body('', '/api/auth/login/'), '')

    def test_top_level_secrets_masked(self):
        out = redact_body('{"username": "ops", "password": "s3cret"}', '/api/auth/login/')
        self.assertIn('ops', out)
        self.assertIn(REDACTED, out)
        self.assertNotIn('s3cret', out)

    def test_key_case_insensitive(self):
        out = redact_body('{"Password": "s3cret", "API_KEY": "k"}', '/api/x/')
        self.assertNotIn('s3cret', out)
        self.assertNotIn('"k"', out)

    def test_nested_and_list(self):
        raw = ('{"items": [{"name": "a", "private_key": "-----BEGIN-----"},'
               ' {"nested": {"become_password": "bp"}}]}')
        out = redact_body(raw, '/api/exec/credentials/')
        self.assertNotIn('BEGIN', out)
        self.assertNotIn('bp', out)
        self.assertIn('"a"', out)

    def test_non_json_auth_path_dropped(self):
        self.assertEqual(redact_body('username=ops&password=s3cret', '/api/auth/login/'),
                         DROPPED)

    def test_non_json_other_path_kept(self):
        self.assertEqual(redact_body('version=1.0.0', '/api/artifacts/report/'),
                         'version=1.0.0')

    def test_truncated_to_limit(self):
        out = redact_body('{"note": "' + 'x' * 5000 + '"}', '/api/x/')
        self.assertEqual(len(out), 2000)


class AuditMiddlewareTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username='auditor')
        self.user.set_password(PASSWORD)
        self.user.save()
        self.client = APIClient()

    def test_login_password_not_stored(self):
        self.client.post('/api/auth/login/',
                         {'username': 'auditor', 'password': PASSWORD}, format='json')
        log = AuditLog.objects.get(path='/api/auth/login/')
        self.assertNotIn(PASSWORD, log.body)
        self.assertIn(REDACTED, log.body)
        self.assertIn('auditor', log.body)  # 用户名仍要留，否则排障没线索
        self.assertEqual(log.status_code, 200)

    def test_failed_login_password_not_stored(self):
        self.client.post('/api/auth/login/',
                         {'username': 'auditor', 'password': 'wrong-pass-1'}, format='json')
        log = AuditLog.objects.get(path='/api/auth/login/')
        self.assertNotIn('wrong-pass-1', log.body)
        self.assertEqual(log.status_code, 401)

    def test_change_password_masked(self):
        login = self.client.post('/api/auth/login/',
                                 {'username': 'auditor', 'password': PASSWORD}, format='json')
        self.client.credentials(HTTP_AUTHORIZATION='Bearer ' + login.json()['access'])
        self.client.post('/api/auth/change-password/',
                         {'old_password': PASSWORD, 'new_password': 'Newp!2345xyz'},
                         format='json')
        log = AuditLog.objects.get(path='/api/auth/change-password/')
        self.assertNotIn(PASSWORD, log.body)
        self.assertNotIn('Newp!2345xyz', log.body)
        self.assertEqual(log.user, self.user)

    def test_multipart_body_not_read(self):
        """制品上报会传数百 MB 的包，审计不该把它读进内存。"""
        self.client.post('/api/artifacts/report/', {'version': '1.0.0'},
                         format='multipart')
        log = AuditLog.objects.get(path='/api/artifacts/report/')
        self.assertEqual(log.body, NOT_RECORDED)

    def test_get_not_recorded(self):
        self.client.get('/api/auth/me/')
        self.assertFalse(AuditLog.objects.exists())
