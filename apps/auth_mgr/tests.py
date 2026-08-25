"""认证测试：登录失败限速（按用户 / 按 IP）、成功清零、缓存故障放行。"""
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

User = get_user_model()
PASSWORD = 'Pass!2345xyz'
WRONG = 'wrong-password-1'

# 阈值调小，避免测试打十几次请求
SMALL_LIMITS = dict(LOGIN_FAIL_LIMIT=3, LOGIN_FAIL_IP_LIMIT=5,
                    LOGIN_FAIL_WINDOW=900, LOGIN_FAIL_BLOCK=900)


@override_settings(**SMALL_LIMITS)
class LoginRateLimitTests(TestCase):
    def setUp(self):
        cache.clear()  # 限速状态在缓存里，用例间必须隔离
        for name in ('alice', 'bob'):
            user = User.objects.create(username=name)
            user.set_password(PASSWORD)
            user.save()
        self.client = APIClient()

    def tearDown(self):
        cache.clear()

    def _login(self, username='alice', password=PASSWORD, ip='10.1.1.1'):
        return self.client.post(
            '/api/auth/login/', {'username': username, 'password': password},
            format='json', REMOTE_ADDR=ip)

    def test_success(self):
        r = self._login()
        self.assertEqual(r.status_code, 200)
        self.assertIn('access', r.json())

    def test_locked_after_limit(self):
        for _ in range(3):
            self.assertEqual(self._login(password=WRONG).status_code, 401)
        r = self._login(password=WRONG)
        self.assertEqual(r.status_code, 429)
        self.assertIn('登录失败次数过多', r.json()['msg'])

    def test_correct_password_still_blocked(self):
        """锁定期内密码正确也不放行，否则限速只是多给攻击者一次机会。"""
        for _ in range(3):
            self._login(password=WRONG)
        self.assertEqual(self._login().status_code, 429)

    def test_success_resets_counter(self):
        for _ in range(2):
            self._login(password=WRONG)
        self.assertEqual(self._login().status_code, 200)
        # 计数已清零：又错 2 次仍不该锁
        for _ in range(2):
            self.assertEqual(self._login(password=WRONG).status_code, 401)

    def test_other_user_unaffected(self):
        for _ in range(3):
            self._login(username='alice', password=WRONG)
        self.assertEqual(self._login(username='alice').status_code, 429)
        # bob 同 IP 但未超 IP 阈值（3 < 5），不该被连坐
        self.assertEqual(self._login(username='bob').status_code, 200)

    def test_username_case_insensitive_bucket(self):
        for _ in range(3):
            self._login(username='Alice', password=WRONG)
        self.assertEqual(self._login(username='alice').status_code, 429)

    def test_ip_bucket_catches_spray(self):
        """同 IP 换用户名喷洒：用户桶各自未满，IP 桶兜住。"""
        for i in range(5):
            self._login(username=f'ghost{i}', password=WRONG)
        self.assertEqual(self._login(username='bob').status_code, 429)

    def test_other_ip_unaffected(self):
        for i in range(5):
            self._login(username=f'ghost{i}', password=WRONG, ip='10.1.1.1')
        self.assertEqual(
            self._login(username='bob', ip='10.2.2.2').status_code, 200)

    def test_forwarded_for_used_as_ip(self):
        for i in range(5):
            self.client.post(
                '/api/auth/login/', {'username': f'ghost{i}', 'password': WRONG},
                format='json', REMOTE_ADDR='10.9.9.9',
                HTTP_X_FORWARDED_FOR='203.0.113.7, 10.9.9.9')
        blocked = self.client.post(
            '/api/auth/login/', {'username': 'bob', 'password': PASSWORD},
            format='json', REMOTE_ADDR='10.9.9.9',
            HTTP_X_FORWARDED_FOR='203.0.113.7, 10.9.9.9')
        self.assertEqual(blocked.status_code, 429)
        # 同一 REMOTE_ADDR 但 XFF 是别的客户端 → 不连坐
        other = self.client.post(
            '/api/auth/login/', {'username': 'bob', 'password': PASSWORD},
            format='json', REMOTE_ADDR='10.9.9.9',
            HTTP_X_FORWARDED_FOR='203.0.113.8, 10.9.9.9')
        self.assertEqual(other.status_code, 200)

    def test_inactive_user_counts_as_failure(self):
        User.objects.filter(username='bob').update(is_active=False)
        for _ in range(3):
            self.assertEqual(self._login(username='bob').status_code, 401)
        self.assertEqual(self._login(username='bob').status_code, 429)

    def test_cache_outage_fails_open(self):
        """缓存挂了要放行：运维平台正是故障时最需要能登进去。"""
        with mock.patch('common.ratelimit.cache') as fake:
            fake.get.side_effect = RuntimeError('redis down')
            fake.add.side_effect = RuntimeError('redis down')
            fake.incr.side_effect = RuntimeError('redis down')
            for _ in range(5):
                self.assertEqual(self._login(password=WRONG).status_code, 401)
            self.assertEqual(self._login().status_code, 200)
