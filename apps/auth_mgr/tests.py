"""认证测试：登录限速、MFA 绑定链路、两步登录、强制策略与管理员重置、API 凭证。"""
import hashlib
import hmac
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import path
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import AccessToken

from apps.audit.models import AuditLog
from apps.task_exec.models import ExecVariable
from apps.system_cfg.models import SystemConfig
from common import totp
from common.response import ok
from common.testing import grant_sudo

from .apikey import ApiKeyAuthentication, ApiKeyThrottle, ensure_service_user, scope_required
from .models import ApiCredential, MFARecoveryCode, Role, UserMFA

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


@override_settings(**SMALL_LIMITS)
class MfaEnrollmentTests(TestCase):
    """绑定链路：setup → activate，以及解绑/恢复码的双确认。"""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create(username='carol')
        self.user.set_password(PASSWORD)
        self.user.save()
        self.client = APIClient()
        self._auth()

    def tearDown(self):
        cache.clear()

    def _auth(self, token=None):
        if token is None:
            r = self.client.post('/api/auth/login/',
                                 {'username': 'carol', 'password': PASSWORD}, format='json')
            token = r.json()['access']
        self.client.credentials(HTTP_AUTHORIZATION='Bearer ' + token)

    @staticmethod
    def _code(secret, offset=0):
        return totp.code_at(secret, totp.current_counter() + offset)

    def test_status_before_setup(self):
        data = self.client.get('/api/auth/mfa/status/').json()['data']
        self.assertFalse(data['enabled'])
        self.assertFalse(data['pending'])
        self.assertEqual(data['recovery_codes_left'], 0)

    def test_setup_returns_secret_uri_and_codes(self):
        data = self.client.post('/api/auth/mfa/setup/').json()['data']
        self.assertTrue(data['secret'])
        self.assertIn('otpauth://totp/', data['uri'])
        self.assertEqual(len(data['recovery_codes']), settings.MFA_RECOVERY_CODES)
        # 密钥加密入库，不是明文
        binding = UserMFA.objects.get(user=self.user)
        self.assertNotIn(data['secret'], binding.secret_enc)
        self.assertEqual(binding.get_secret(), data['secret'])
        self.assertFalse(binding.enabled)  # 未激活前不生效
        # 恢复码只存哈希
        for row in MFARecoveryCode.objects.filter(user=self.user):
            self.assertEqual(len(row.code_hash), 64)
            for code in data['recovery_codes']:
                self.assertNotIn(code.replace('-', ''), row.code_hash)

    def test_activate_with_wrong_code_keeps_disabled(self):
        self.client.post('/api/auth/mfa/setup/')
        r = self.client.post('/api/auth/mfa/activate/', {'code': '000000'}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertFalse(UserMFA.objects.get(user=self.user).enabled)

    def test_activate_success_returns_new_tokens(self):
        secret = self.client.post('/api/auth/mfa/setup/').json()['data']['secret']
        r = self.client.post('/api/auth/mfa/activate/',
                             {'code': self._code(secret)}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(UserMFA.objects.get(user=self.user).enabled)
        # 新令牌带 otp 声明，强制策略下当前会话不会被自己挡住
        access = AccessToken(r.json()['data']['access'])
        self.assertEqual(access['amr'], ['pwd', 'otp'])

    def test_setup_rejected_when_already_enabled(self):
        secret = self.client.post('/api/auth/mfa/setup/').json()['data']['secret']
        self.client.post('/api/auth/mfa/activate/', {'code': self._code(secret)},
                         format='json')
        r = self.client.post('/api/auth/mfa/setup/')
        self.assertEqual(r.status_code, 400)
        self.assertIn('已绑定', r.json()['msg'])

    def test_disable_requires_password_and_code(self):
        secret = self._enable()
        bad = self.client.post('/api/auth/mfa/disable/',
                               {'password': 'nope', 'code': self._code(secret)},
                               format='json')
        self.assertEqual(bad.status_code, 400)
        self.assertTrue(UserMFA.objects.filter(user=self.user).exists())
        ok_resp = self.client.post('/api/auth/mfa/disable/',
                                   {'password': PASSWORD, 'code': self._code(secret, 1)},
                                   format='json')
        self.assertEqual(ok_resp.status_code, 200)
        self.assertFalse(UserMFA.objects.filter(user=self.user).exists())
        self.assertFalse(MFARecoveryCode.objects.filter(user=self.user).exists())

    def test_regenerate_recovery_codes_invalidates_old(self):
        secret = self._enable()
        old = list(MFARecoveryCode.objects.filter(user=self.user)
                   .values_list('code_hash', flat=True))
        r = self.client.post('/api/auth/mfa/recovery-codes/',
                             {'password': PASSWORD, 'code': self._code(secret, 1)},
                             format='json')
        self.assertEqual(r.status_code, 200)
        new = list(MFARecoveryCode.objects.filter(user=self.user)
                   .values_list('code_hash', flat=True))
        self.assertEqual(len(new), settings.MFA_RECOVERY_CODES)
        self.assertFalse(set(old) & set(new))

    def _enable(self):
        secret = self.client.post('/api/auth/mfa/setup/').json()['data']['secret']
        r = self.client.post('/api/auth/mfa/activate/', {'code': self._code(secret)},
                             format='json')
        self._auth(r.json()['data']['access'])
        return secret


@override_settings(LOGIN_FAIL_LIMIT=30, LOGIN_FAIL_IP_LIMIT=99,
                   LOGIN_FAIL_WINDOW=900, LOGIN_FAIL_BLOCK=900,
                   MFA_MAX_ATTEMPTS=3)
class MfaLoginTests(TestCase):
    """两步登录：挑战票据、验证码/恢复码、防重放、票据一次性与错误次数。

    限速阈值放宽：本类专测票据机制，限速与 MFA 的联动单独在
    MfaRateLimitTests 里验。激活用的是当前时间窗的码（last_counter 已推进），
    所以后续用例一律取 +1 窗口的码——同一个窗口的码用过就不再认（防重放）。
    """

    def setUp(self):
        cache.clear()
        self.user = User.objects.create(username='dave')
        self.user.set_password(PASSWORD)
        self.user.save()
        self.client = APIClient()
        self.secret, self.recovery = self._enable_mfa()
        self.client.credentials()  # 后续用例从未登录态开始

    def tearDown(self):
        cache.clear()

    def _enable_mfa(self):
        r = self.client.post('/api/auth/login/',
                             {'username': 'dave', 'password': PASSWORD}, format='json')
        self.client.credentials(HTTP_AUTHORIZATION='Bearer ' + r.json()['access'])
        data = self.client.post('/api/auth/mfa/setup/').json()['data']
        self.client.post('/api/auth/mfa/activate/',
                         {'code': self._code(data['secret'])}, format='json')
        return data['secret'], data['recovery_codes']

    @staticmethod
    def _code(secret, offset=0):
        return totp.code_at(secret, totp.current_counter() + offset)

    def _password_step(self):
        r = self.client.post('/api/auth/login/',
                             {'username': 'dave', 'password': PASSWORD}, format='json')
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_password_step_returns_challenge_not_tokens(self):
        body = self._password_step()
        self.assertTrue(body['mfa_required'])
        self.assertNotIn('access', body)
        self.assertNotIn('refresh', body)
        self.assertEqual(body['expires_in'], settings.MFA_CHALLENGE_TTL)

    def test_challenge_token_is_not_usable_as_access_token(self):
        """票据 token_type='mfa'，当 Bearer 用必须被拒，否则第一步就等于登录成功。"""
        body = self._password_step()
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION='Bearer ' + body['mfa_token'])
        self.assertEqual(client.get('/api/auth/me/').status_code, 401)

    def test_verify_returns_tokens_with_otp_amr(self):
        body = self._password_step()
        r = self.client.post('/api/auth/mfa/verify/',
                             {'mfa_token': body['mfa_token'],
                              'code': self._code(self.secret, 1)}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(AccessToken(r.json()['access'])['amr'], ['pwd', 'otp'])

    def test_wrong_code_then_correct(self):
        body = self._password_step()
        bad = self.client.post('/api/auth/mfa/verify/',
                               {'mfa_token': body['mfa_token'], 'code': '000000'},
                               format='json')
        self.assertEqual(bad.status_code, 400)
        self.assertIn('还可尝试', bad.json()['msg'])
        good = self.client.post('/api/auth/mfa/verify/',
                                {'mfa_token': body['mfa_token'],
                                 'code': self._code(self.secret, 1)}, format='json')
        self.assertEqual(good.status_code, 200)

    def test_challenge_single_use(self):
        body = self._password_step()
        payload = {'mfa_token': body['mfa_token'], 'code': self._code(self.secret, 1)}
        self.assertEqual(self.client.post('/api/auth/mfa/verify/', payload,
                                          format='json').status_code, 200)
        again = self.client.post('/api/auth/mfa/verify/', payload, format='json')
        self.assertEqual(again.status_code, 401)
        self.assertIn('已使用', again.json()['msg'])

    def test_code_replay_blocked_on_new_challenge(self):
        """同一个 30 秒窗口的码用过一次就不能再用，即便换一张新票据。"""
        code = self._code(self.secret, 1)
        first = self._password_step()
        self.assertEqual(self.client.post(
            '/api/auth/mfa/verify/', {'mfa_token': first['mfa_token'], 'code': code},
            format='json').status_code, 200)
        second = self._password_step()
        replay = self.client.post('/api/auth/mfa/verify/',
                                  {'mfa_token': second['mfa_token'], 'code': code},
                                  format='json')
        self.assertEqual(replay.status_code, 400)

    def test_attempts_exhausted_invalidates_challenge(self):
        body = self._password_step()
        for _ in range(settings.MFA_MAX_ATTEMPTS - 1):
            self.assertEqual(self.client.post(
                '/api/auth/mfa/verify/',
                {'mfa_token': body['mfa_token'], 'code': '000000'},
                format='json').status_code, 400)
        last = self.client.post('/api/auth/mfa/verify/',
                                {'mfa_token': body['mfa_token'], 'code': '000000'},
                                format='json')
        self.assertEqual(last.status_code, 401)
        # 票据已作废：正确的码也不能再用它
        after = self.client.post('/api/auth/mfa/verify/',
                                 {'mfa_token': body['mfa_token'],
                                  'code': self._code(self.secret, 1)}, format='json')
        self.assertEqual(after.status_code, 401)

    def test_recovery_code_login_and_single_use(self):
        code = self.recovery[0]
        body = self._password_step()
        r = self.client.post('/api/auth/mfa/verify/',
                             {'mfa_token': body['mfa_token'], 'code': code},
                             format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['recovery_codes_left'],
                         settings.MFA_RECOVERY_CODES - 1)
        again = self._password_step()
        reuse = self.client.post('/api/auth/mfa/verify/',
                                 {'mfa_token': again['mfa_token'], 'code': code},
                                 format='json')
        self.assertEqual(reuse.status_code, 400)

    def test_recovery_code_accepts_without_dashes(self):
        code = self.recovery[1].replace('-', '').lower()
        body = self._password_step()
        r = self.client.post('/api/auth/mfa/verify/',
                             {'mfa_token': body['mfa_token'], 'code': code},
                             format='json')
        self.assertEqual(r.status_code, 200)

    def test_expired_challenge_rejected(self):
        with override_settings(MFA_CHALLENGE_TTL=-1):
            body = self._password_step()
            r = self.client.post('/api/auth/mfa/verify/',
                                 {'mfa_token': body['mfa_token'],
                                  'code': self._code(self.secret, 1)}, format='json')
        self.assertEqual(r.status_code, 401)

    def test_wrong_password_never_reaches_mfa(self):
        r = self.client.post('/api/auth/login/',
                             {'username': 'dave', 'password': WRONG}, format='json')
        self.assertEqual(r.status_code, 401)




@override_settings(LOGIN_FAIL_LIMIT=3, LOGIN_FAIL_IP_LIMIT=99,
                   LOGIN_FAIL_WINDOW=900, LOGIN_FAIL_BLOCK=900,
                   MFA_MAX_ATTEMPTS=99)
class MfaRateLimitTests(TestCase):
    """换票据重试也受限：票据内计次之外还有按用户/IP 的失败计数。

    票据次数放到 99、限速阈值压到 3，好让限速先触发（现实里两者都在，先撞
    哪个取决于配置）。
    """

    def setUp(self):
        cache.clear()
        user = User.objects.create(username='ivan')
        user.set_password(PASSWORD)
        user.save()
        self.client = APIClient()
        r = self.client.post('/api/auth/login/',
                             {'username': 'ivan', 'password': PASSWORD}, format='json')
        self.client.credentials(HTTP_AUTHORIZATION='Bearer ' + r.json()['access'])
        data = self.client.post('/api/auth/mfa/setup/').json()['data']
        self.secret = data['secret']
        self.client.post('/api/auth/mfa/activate/',
                         {'code': totp.code_at(self.secret, totp.current_counter())},
                         format='json')
        self.client.credentials()

    def tearDown(self):
        cache.clear()

    def _password_step(self):
        return self.client.post('/api/auth/login/',
                                {'username': 'ivan', 'password': PASSWORD},
                                format='json').json()

    def _code(self, secret, offset=0):
        return totp.code_at(secret, totp.current_counter() + offset)

    def test_mfa_failures_count_toward_ratelimit(self):
        for _ in range(3):
            body = self._password_step()
            self.client.post('/api/auth/mfa/verify/',
                             {'mfa_token': body['mfa_token'], 'code': '000000'},
                             format='json')
        blocked = self.client.post('/api/auth/mfa/verify/',
                                   {'mfa_token': self._password_step()['mfa_token'],
                                    'code': self._code(self.secret, 1)}, format='json')
        self.assertEqual(blocked.status_code, 429)


class MfaEnforcementTests(TestCase):
    """强制策略：scope=off/staff/all 下未绑定用户的可达范围。"""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create(username='erin', is_staff=False)
        self.user.set_password(PASSWORD)
        self.user.save()
        self.admin = User.objects.create(username='frank', is_staff=True)
        self.admin.set_password(PASSWORD)
        self.admin.save()

    def tearDown(self):
        cache.clear()

    def _scope(self, value):
        SystemConfig.objects.update_or_create(
            key='mfa.enforce_scope', defaults={'value': value})

    def _client(self, username='erin'):
        client = APIClient()
        r = client.post('/api/auth/login/',
                        {'username': username, 'password': PASSWORD}, format='json')
        client.credentials(HTTP_AUTHORIZATION='Bearer ' + r.json()['access'])
        return client, r.json()

    def test_default_scope_off_allows_everything(self):
        client, body = self._client()
        self.assertNotIn('mfa_enrollment_required', body)
        self.assertEqual(client.get('/api/assets/servers/').status_code, 200)

    def test_scope_all_blocks_unbound_user(self):
        self._scope('all')
        client, body = self._client()
        self.assertTrue(body['mfa_enrollment_required'])
        r = client.get('/api/assets/servers/')
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()['error_code'], 'mfa_enrollment_required')

    def test_scope_all_still_allows_enrollment_endpoints(self):
        """否则就是死锁：被要求绑定，却连绑定接口都调不了。"""
        self._scope('all')
        client, _ = self._client()
        self.assertEqual(client.get('/api/auth/me/').status_code, 200)
        self.assertEqual(client.get('/api/auth/mfa/status/').status_code, 200)
        self.assertEqual(client.post('/api/auth/mfa/setup/').status_code, 200)

    def test_scope_staff_targets_only_staff(self):
        self._scope('staff')
        member, _ = self._client('erin')
        self.assertEqual(member.get('/api/assets/servers/').status_code, 200)
        admin, body = self._client('frank')
        self.assertTrue(body['mfa_enrollment_required'])
        self.assertEqual(admin.get('/api/assets/servers/').status_code, 403)

    def test_invalid_scope_falls_back_to_off(self):
        """配置值拼错不该把全员挡在门外。"""
        self._scope('ALL_USERS')
        client, _ = self._client()
        self.assertEqual(client.get('/api/assets/servers/').status_code, 200)

    def test_bound_user_passes_after_verify(self):
        self._scope('all')
        client, _ = self._client()
        data = client.post('/api/auth/mfa/setup/').json()['data']
        activated = client.post('/api/auth/mfa/activate/',
                                {'code': totp.code_at(data['secret'],
                                                      totp.current_counter())},
                                format='json')
        client.credentials(HTTP_AUTHORIZATION='Bearer '
                           + activated.json()['data']['access'])
        self.assertEqual(client.get('/api/assets/servers/').status_code, 200)


class MfaAdminResetTests(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = User.objects.create(username='gina', is_staff=True)
        self.admin.set_password(PASSWORD)
        self.admin.save()
        self.target = User.objects.create(username='hank')
        self.target.set_password(PASSWORD)
        self.target.save()
        binding = UserMFA(user=self.target, enabled=True)
        binding.set_secret(totp.random_secret())
        binding.save()
        MFARecoveryCode.objects.create(user=self.target, code_hash='x' * 64)
        self.client = APIClient()
        r = self.client.post('/api/auth/login/',
                             {'username': 'gina', 'password': PASSWORD}, format='json')
        self.client.credentials(HTTP_AUTHORIZATION='Bearer ' + r.json()['access'])
        # 用户管理（含 reset_mfa）要求 sudo 二次验证
        grant_sudo(self.client, PASSWORD)

    def tearDown(self):
        cache.clear()

    def test_admin_can_reset(self):
        r = self.client.post(f'/api/auth/users/{self.target.pk}/reset_mfa/')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(UserMFA.objects.filter(user=self.target).exists())
        self.assertFalse(MFARecoveryCode.objects.filter(user=self.target).exists())

    def test_reset_unbound_user_reports(self):
        self.client.post(f'/api/auth/users/{self.target.pk}/reset_mfa/')
        r = self.client.post(f'/api/auth/users/{self.target.pk}/reset_mfa/')
        self.assertEqual(r.status_code, 400)

    def test_non_admin_forbidden(self):
        client = APIClient()
        r = client.post('/api/auth/login/',
                        {'username': 'hank', 'password': PASSWORD}, format='json')
        # hank 已启用 MFA，密码步只拿到挑战票据，够不到管理接口
        self.assertTrue(r.json()['mfa_required'])

    def test_user_list_shows_mfa_state(self):
        rows = self.client.get('/api/auth/users/').json()['data']['results']
        states = {row['username']: row['mfa_enabled'] for row in rows}
        self.assertTrue(states['hank'])
        self.assertFalse(states['gina'])

    def test_management_command_resets(self):
        from django.core.management import call_command
        call_command('mfa_reset', 'hank', '--yes')
        self.assertFalse(UserMFA.objects.filter(user=self.target).exists())

    def test_management_command_check_is_read_only(self):
        """--check 只报状态：部署前验证兜底通道时不能顺手清掉真绑定。"""
        from io import StringIO

        from django.core.management import call_command
        out = StringIO()
        call_command('mfa_reset', 'hank', '--check', stdout=out)
        self.assertIn('已启用', out.getvalue())
        self.assertTrue(UserMFA.objects.filter(user=self.target).exists())


@override_settings(SUDO_TTL=600)
class SudoTests(TestCase):
    """敏感操作二次验证：窗口开启/过期、绑 token、fail-closed、身份变更失效。"""

    def setUp(self):
        cache.clear()
        self.admin = User.objects.create(username='sudo_admin', is_staff=True)
        self.admin.set_password(PASSWORD)
        self.admin.save()
        self.client = APIClient()
        self.access = self._login()

    def tearDown(self):
        cache.clear()

    def _login(self, client=None):
        client = client or self.client
        r = client.post('/api/auth/login/',
                        {'username': 'sudo_admin', 'password': PASSWORD}, format='json')
        access = r.json()['access']
        client.credentials(HTTP_AUTHORIZATION='Bearer ' + access)
        return access

    def _grant(self, **payload):
        return self.client.post('/api/auth/mfa/sudo/', payload or {'password': PASSWORD},
                                format='json')

    # ── 拦截与放行 ────────────────────────────────────────

    def test_write_blocked_without_sudo(self):
        r = self.client.post('/api/exec/credentials/',
                             {'name': 'c1', 'username': 'root', 'auth_type': 'password',
                              'password': 'x'}, format='json')
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()['error_code'], 'sudo_required')

    def test_read_allowed_without_sudo(self):
        """只读不该被卡：sudo 防的是持久权限扩大，不是看一眼。"""
        self.assertEqual(self.client.get('/api/exec/credentials/').status_code, 200)

    def test_write_allowed_after_grant(self):
        self.assertEqual(self._grant().status_code, 200)
        r = self.client.post('/api/exec/credentials/',
                             {'name': 'c2', 'username': 'root', 'auth_type': 'password',
                              'password': 'x'}, format='json')
        self.assertEqual(r.status_code, 201)

    def test_all_config_surfaces_covered(self):
        """配置面五处都要拦：漏一个就等于这道门没关。"""
        cases = [
            ('/api/auth/users/', {'username': 'someone'}),
            ('/api/exec/credentials/', {'name': 'c3', 'username': 'root',
                                        'auth_type': 'password', 'password': 'x'}),
            ('/api/agents/', {'name': 'gw-sudo'}),
            ('/api/agent-routes/', {'agent': 1, 'project_group': 1}),
            ('/api/cloud/accounts/', {'name': 'acc'}),
            ('/api/system/configs/', {'key': 'k', 'value': 'v'}),
        ]
        for path, payload in cases:
            with self.subTest(path=path):
                r = self.client.post(path, payload, format='json')
                self.assertEqual(r.status_code, 403, path)
                self.assertEqual(r.json()['error_code'], 'sudo_required', path)

    def test_server_write_gated_but_create_open(self):
        """服务器改/删要 sudo（channel_mode 决定命令下发到哪台机器），新增不拦。"""
        from apps.asset_mgr.models import Server

        srv = Server.objects.create(name='s-sudo', private_ip='10.9.9.9')
        blocked = self.client.patch(f'/api/assets/servers/{srv.pk}/',
                                    {'channel_mode': 'direct'}, format='json')
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(blocked.json()['error_code'], 'sudo_required')
        self.assertEqual(
            self.client.delete(f'/api/assets/servers/{srv.pk}/').status_code, 403)
        # 新增不拦：新加的主机不会改变已有主机的执行通道
        created = self.client.post('/api/assets/servers/',
                                   {'name': 's-new', 'private_ip': '10.9.9.10'},
                                   format='json')
        self.assertEqual(created.status_code, 201)

    def test_server_batch_and_import_gated(self):
        for path in ('/api/assets/servers/batch/update/',
                     '/api/assets/servers/batch/delete/'):
            with self.subTest(path=path):
                r = self.client.post(path, {'ids': [1]}, format='json')
                self.assertEqual(r.status_code, 403, path)
                self.assertEqual(r.json()['error_code'], 'sudo_required', path)

    def test_cmdb_dicts_not_gated(self):
        """字典改的是分类归属，不扩权，刻意不纳入（400=校验错，说明没被拦）。"""
        r = self.client.post('/api/assets/project-groups/', {}, format='json')
        self.assertEqual(r.status_code, 400)

    def test_task_execution_not_gated(self):
        """任务流执行刻意不纳入：否则密集发布时反复弹验证框。"""
        r = self.client.get('/api/exec/tasks/')
        self.assertEqual(r.status_code, 200)

    # ── 凭证校验 ──────────────────────────────────────────

    def test_wrong_password_rejected(self):
        r = self._grant(password='nope')
        self.assertEqual(r.status_code, 400)
        self.assertIn('密码不正确', r.json()['msg'])

    def test_password_required_when_no_mfa(self):
        r = self._grant(code='123456')
        self.assertEqual(r.status_code, 400)
        self.assertIn('登录密码', r.json()['msg'])

    def test_mfa_user_must_use_code(self):
        """启用了 MFA 就不接受密码：密码钓得到，验证器钓不到。"""
        binding = UserMFA(user=self.admin, enabled=True)
        secret = totp.random_secret()
        binding.set_secret(secret)
        binding.save()
        self.assertEqual(self._grant(password=PASSWORD).status_code, 400)
        good = self._grant(code=totp.code_at(secret, totp.current_counter()))
        self.assertEqual(good.status_code, 200)

    def test_repeated_submit_is_idempotent(self):
        """窗口已开着时重复提交同一个码返回成功，而不是「验证码不正确」。

        前端满 6 位自动提交 + 用户再回车会打两次，第二次拿同码必然撞防重放，
        报错却其实已放行——用户看到的就是「提示验证码错，但删除成功了」。
        """
        binding = UserMFA(user=self.admin, enabled=True)
        secret = totp.random_secret()
        binding.set_secret(secret)
        binding.save()
        code = totp.code_at(secret, totp.current_counter())
        first = self._grant(code=code)
        second = self._grant(code=code)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertIn('已生效', second.json()['msg'])

    def test_replayed_code_says_so(self):
        """码正确但窗口用过：文案要说「已使用过」，不能说「不正确」。

        登录刚用掉当前窗口的码，紧接着开 sudo 会撞上；说成「不正确」会让人
        反复怀疑密钥或手机时钟。
        """
        binding = UserMFA(user=self.admin, enabled=True)
        secret = totp.random_secret()
        binding.set_secret(secret)
        counter = totp.current_counter()
        binding.last_counter = counter          # 该窗口已被登录消费
        binding.save()
        r = self._grant(code=totp.code_at(secret, counter))
        self.assertEqual(r.status_code, 400)
        self.assertIn('已使用过', r.json()['msg'])
        # 下一个窗口的码照常可用
        self.assertEqual(
            self._grant(code=totp.code_at(secret, counter + 1)).status_code, 200)

    def test_failures_rate_limited(self):
        with override_settings(LOGIN_FAIL_LIMIT=3):
            for _ in range(3):
                self.assertEqual(self._grant(password='nope').status_code, 400)
            self.assertEqual(self._grant(password='nope').status_code, 429)

    # ── 窗口边界 ──────────────────────────────────────────

    def test_window_expires(self):
        with override_settings(SUDO_TTL=-1):   # 立即过期
            self.assertEqual(self._grant().status_code, 200)
        r = self.client.post('/api/system/configs/', {'key': 'k', 'value': 'v'},
                             format='json')
        self.assertEqual(r.status_code, 403)

    def test_window_bound_to_access_token(self):
        """换 token（静默刷新/另一条会话）不继承授权。"""
        self._grant()
        other = APIClient()
        self._login(other)
        r = other.post('/api/system/configs/', {'key': 'k2', 'value': 'v'}, format='json')
        self.assertEqual(r.status_code, 403)

    def test_status_endpoint(self):
        before = self.client.get('/api/auth/mfa/sudo/').json()['data']
        self.assertFalse(before['active'])
        self.assertFalse(before['mfa_enabled'])
        self._grant()
        after = self.client.get('/api/auth/mfa/sudo/').json()['data']
        self.assertTrue(after['active'])
        self.assertEqual(after['ttl'], 600)

    def test_change_password_revokes_window(self):
        self._grant()
        self.client.post('/api/auth/change-password/',
                         {'old_password': PASSWORD, 'new_password': 'Newp!2345xyz'},
                         format='json')
        r = self.client.post('/api/system/configs/', {'key': 'k3', 'value': 'v'},
                             format='json')
        self.assertEqual(r.status_code, 403)

    def test_cache_outage_denies(self):
        """fail-closed：缓存挂了拒绝敏感操作（与限速的 fail-open 相反）。"""
        self._grant()
        with mock.patch('common.sudo.cache') as fake:
            fake.get.side_effect = RuntimeError('redis down')
            r = self.client.post('/api/system/configs/', {'key': 'k4', 'value': 'v'},
                                 format='json')
        self.assertEqual(r.status_code, 403)


# ── API 凭证（机读接口访问控制）─────────────────────────────
class _ProbeView(APIView):
    """测试专用的机读接口：只挂 API 凭证这一套，用来单独验校验链。

    不借用制品上报/任务触发做断言：那两个接口各自还有业务前置校验（multipart
    上传、任务必须配好模块），失败原因会和访问控制的失败混在一起。
    """

    authentication_classes = [ApiKeyAuthentication]
    permission_classes = [scope_required('task:trigger')]
    throttle_classes = [ApiKeyThrottle]

    def post(self, request):
        return ok({'username': request.user.username, 'key_id': request.auth.key_id})


#: 供 override_settings(ROOT_URLCONF=__name__) 使用
urlpatterns = [path('probe/', _ProbeView.as_view(), name='probe')]

PROBE = '/probe/'
TRUSTED = ['127.0.0.1']


def make_credential(name='ci', scopes=('task:trigger',), **kwargs):
    """建一把凭证，返回 (凭证, 明文 key)。"""
    raw, key_id, secret_hash = ApiCredential.generate_key()
    defaults = dict(ip_whitelist='203.0.113.9', ip_unrestricted=False)
    defaults.update(kwargs)
    credential = ApiCredential.objects.create(
        name=name, key_id=key_id, secret_hash=secret_hash, scopes=list(scopes),
        bound_user=ensure_service_user(name), **defaults)
    return credential, raw


@override_settings(ROOT_URLCONF=__name__, TRUSTED_PROXIES=TRUSTED)
class ApiKeyAuthTests(TestCase):
    """凭证校验链：key、状态、过期、IP 白名单、作用域、限速。"""

    def setUp(self):
        cache.clear()  # 失败计数与限速桶在缓存里，用例间必须隔离
        self.client = APIClient()
        self.credential, self.key = make_credential()

    def tearDown(self):
        cache.clear()

    def _post(self, key=None, ip='203.0.113.9', **extra):
        if key is not None:
            extra['HTTP_X_API_KEY'] = key
        return self.client.post(PROBE, {}, format='json', REMOTE_ADDR=ip, **extra)

    # ── key 本身 ──────────────────────────────────────────
    def test_valid_key(self):
        r = self._post(self.key)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['data']['key_id'], self.credential.key_id)
        self.assertEqual(r.json()['data']['username'], 'svc-ci')

    def test_no_key_is_401(self):
        self.assertEqual(self._post().status_code, 401)

    def test_wrong_secret_is_401(self):
        key_id, _ = ApiCredential.split_key(self.key)
        r = self._post(f'{key_id}.wrong-secret')
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()['error_code'], 'api_key_invalid')

    def test_unknown_key_id_same_error_as_wrong_secret(self):
        """两者返回同一个错误码：否则枚举 key_id 就能探出哪些凭证存在。"""
        r = self._post('ak_ffffffffffffffff.whatever')
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()['error_code'], 'api_key_invalid')

    def test_legacy_format_key_not_accepted(self):
        """不是 ak_ 开头的走不到凭证这条链（留给旧 key 的兜底权限类）。"""
        self.assertEqual(self._post('some-legacy-global-key').status_code, 401)

    def test_authorization_apikey_header(self):
        r = self.client.post(PROBE, {}, format='json', REMOTE_ADDR='203.0.113.9',
                             HTTP_AUTHORIZATION=f'ApiKey {self.key}')
        self.assertEqual(r.status_code, 200)

    def test_rotate_invalidates_old_key(self):
        new_key = self.credential.rotate_key()
        self.assertEqual(self._post(self.key).status_code, 401)
        self.assertEqual(self._post(new_key).status_code, 200)

    # ── 状态 ──────────────────────────────────────────────
    def test_disabled_is_401(self):
        self.credential.enabled = False
        self.credential.save(update_fields=['enabled'])
        self.assertEqual(self._post(self.key).status_code, 401)

    def test_expired_is_401(self):
        self.credential.expires_at = timezone.now() - timedelta(seconds=1)
        self.credential.save(update_fields=['expires_at'])
        r = self._post(self.key)
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()['error_code'], 'api_key_expired')

    def test_disabled_service_account_is_401(self):
        self.credential.bound_user.is_active = False
        self.credential.bound_user.save(update_fields=['is_active'])
        self.assertEqual(self._post(self.key).status_code, 401)

    # ── IP 白名单 ─────────────────────────────────────────
    def test_ip_outside_whitelist_is_403(self):
        r = self._post(self.key, ip='198.51.100.7')
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()['error_code'], 'api_key_ip_denied')

    def test_forged_xff_cannot_bypass_whitelist(self):
        """直连（对端不在 TRUSTED_PROXIES）时伪造 XFF 必须无效。

        这是整套白名单成立的前提：XFF 是普通请求头，客户端想写什么写什么。
        """
        r = self._post(self.key, ip='198.51.100.7',
                       HTTP_X_FORWARDED_FOR='203.0.113.9')
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()['error_code'], 'api_key_ip_denied')

    def test_trusted_proxy_xff_accepted(self):
        r = self._post(self.key, ip='127.0.0.1',
                       HTTP_X_FORWARDED_FOR='10.0.0.5, 203.0.113.9')
        self.assertEqual(r.status_code, 200)

    def test_cidr_whitelist(self):
        self.credential.ip_whitelist = '203.0.113.0/24'
        self.credential.save(update_fields=['ip_whitelist'])
        self.assertEqual(self._post(self.key, ip='203.0.113.200').status_code, 200)

    def test_empty_whitelist_denies_everything(self):
        """留空不等于不限制：没勾「不限制」就一律拒。"""
        self.credential.ip_whitelist = ''
        self.credential.save(update_fields=['ip_whitelist'])
        self.assertEqual(self._post(self.key, ip='203.0.113.9').status_code, 403)

    def test_unrestricted_allows_any_ip(self):
        self.credential.ip_unrestricted = True
        self.credential.save(update_fields=['ip_unrestricted'])
        self.assertEqual(self._post(self.key, ip='198.51.100.7').status_code, 200)

    # ── 作用域 ────────────────────────────────────────────
    def test_missing_scope_is_403(self):
        self.credential.scopes = ['artifact:report']
        self.credential.save(update_fields=['scopes'])
        r = self._post(self.key)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()['error_code'], 'api_key_scope_denied')

    # ── 限速与暴破 ────────────────────────────────────────
    @override_settings(API_KEY_FAIL_LIMIT=3, API_KEY_FAIL_WINDOW=300,
                       API_KEY_FAIL_BLOCK=300, ROOT_URLCONF=__name__,
                       TRUSTED_PROXIES=TRUSTED)
    def test_brute_force_locks_key(self):
        key_id, _ = ApiCredential.split_key(self.key)
        for _ in range(3):
            self.assertEqual(self._post(f'{key_id}.wrong').status_code, 401)
        # 锁定期内即使 key 正确也不放行，否则限速只是多给攻击者一次机会
        self.assertEqual(self._post(self.key).status_code, 429)

    @override_settings(REST_FRAMEWORK_THROTTLE_UNUSED=None)
    def test_rate_limit_from_credential(self):
        self.credential.rate_limit = '2/min'
        self.credential.save(update_fields=['rate_limit'])
        self.assertEqual(self._post(self.key).status_code, 200)
        self.assertEqual(self._post(self.key).status_code, 200)
        self.assertEqual(self._post(self.key).status_code, 429)

    # ── 使用痕迹与审计 ────────────────────────────────────
    def test_last_used_recorded(self):
        self._post(self.key)
        self.credential.refresh_from_db()
        self.assertIsNotNone(self.credential.last_used_at)
        self.assertEqual(self.credential.last_used_ip, '203.0.113.9')

    def test_audit_records_service_account(self):
        """审计要能回答「这次机读调用是哪把凭证」——靠绑定的服务账号落 FK。"""
        self._post(self.key)
        entry = AuditLog.objects.filter(path=PROBE).first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.user, self.credential.bound_user)


@override_settings(ROOT_URLCONF=__name__, TRUSTED_PROXIES=TRUSTED)
class WebhookSignatureTests(TestCase):
    """Webhook 签名：HMAC-SHA256（签原始请求体）与共享 Token 两种模式。"""

    SECRET = 'webhook-signing-secret-0001'
    BODY = b'{"ref":"refs/heads/main"}'

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.credential, self.key = make_credential(
            name='gh-webhook', sig_mode=ApiCredential.SIG_HMAC_SHA256,
            sig_header='X-Hub-Signature-256', sig_prefix='sha256=')
        self.credential.set_signing_secret(self.SECRET)
        self.credential.save(update_fields=['signing_secret_enc'])

    def tearDown(self):
        cache.clear()

    def _sig(self, secret=None, body=None):
        return 'sha256=' + hmac.new((secret or self.SECRET).encode(),
                                    body or self.BODY, hashlib.sha256).hexdigest()

    def _post(self, signature=None, body=None, header='HTTP_X_HUB_SIGNATURE_256'):
        extra = {'HTTP_X_API_KEY': self.key, 'REMOTE_ADDR': '203.0.113.9'}
        if signature is not None:
            extra[header] = signature
        return self.client.post(PROBE, data=body or self.BODY,
                                content_type='application/json', **extra)

    def test_valid_signature(self):
        self.assertEqual(self._post(self._sig()).status_code, 200)

    def test_missing_signature_header_is_403(self):
        r = self._post()
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()['error_code'], 'api_key_signature_invalid')

    def test_wrong_secret_is_403(self):
        self.assertEqual(self._post(self._sig(secret='other')).status_code, 403)

    def test_tampered_body_is_403(self):
        """签的是原始字节：体改一个字符签名就不成立。"""
        signature = self._sig()
        r = self._post(signature, body=b'{"ref":"refs/heads/prod"}')
        self.assertEqual(r.status_code, 403)

    def test_signature_without_prefix_also_accepted(self):
        """sig_prefix 只是剥前缀，纯 hex 的提供方一样能用。"""
        raw_hex = self._sig().split('=', 1)[1]
        self.assertEqual(self._post(raw_hex).status_code, 200)

    def test_token_mode(self):
        self.credential.sig_mode = ApiCredential.SIG_TOKEN
        self.credential.sig_header = 'X-Gitlab-Token'
        self.credential.save(update_fields=['sig_mode', 'sig_header'])
        self.assertEqual(
            self._post(self.SECRET, header='HTTP_X_GITLAB_TOKEN').status_code, 200)
        self.assertEqual(
            self._post('nope', header='HTTP_X_GITLAB_TOKEN').status_code, 403)

    def test_signature_required_even_if_view_forgets(self):
        """验签在认证层：视图没挂任何签名相关的权限类也照样必过。"""
        self.assertEqual(len(_ProbeView.permission_classes), 1)
        self.assertEqual(self._post().status_code, 403)


class ApiCredentialAdminTests(TestCase):
    """凭证管理接口：权限、sudo、明文只出现一次、白名单校验。"""

    URL = '/api/auth/api-credentials/'

    def setUp(self):
        cache.clear()
        self.admin = User.objects.create(username='cred_admin', is_staff=True)
        self.admin.set_password(PASSWORD)
        self.admin.save()
        self.client = self._login('cred_admin')
        grant_sudo(self.client, PASSWORD)

    def tearDown(self):
        cache.clear()

    def _login(self, username):
        client = APIClient()
        r = client.post('/api/auth/login/', {'username': username, 'password': PASSWORD},
                        format='json')
        client.credentials(HTTP_AUTHORIZATION='Bearer ' + r.json()['access'])
        return client

    def _payload(self, **kwargs):
        data = {'name': 'ci-jenkins', 'scopes': ['artifact:report'],
                'ip_whitelist': '10.0.1.0/24'}
        data.update(kwargs)
        return data

    # ── 创建 ──────────────────────────────────────────────
    def test_create_returns_plaintext_once(self):
        r = self.client.post(self.URL, self._payload(), format='json')
        self.assertEqual(r.status_code, 200, r.content)
        data = r.json()['data']
        raw = data['api_key']
        self.assertTrue(raw.startswith('ak_'))
        # 明文不入库，只留哈希
        credential = ApiCredential.objects.get(pk=data['id'])
        self.assertNotIn(raw, [credential.secret_hash, credential.key_id])
        self.assertTrue(credential.verify_secret(raw.split('.', 1)[1]))
        # 列表与详情都不再给明文（该 ViewSet 关了分页，list 直接返回数组）
        self.assertNotIn('api_key', self.client.get(self.URL).json()[0])
        self.assertNotIn('api_key', self.client.get(f'{self.URL}{data["id"]}/').json())

    def test_create_auto_binds_service_account(self):
        r = self.client.post(self.URL, self._payload(), format='json')
        self.assertEqual(r.json()['data']['bound_user_name'], 'svc-ci-jenkins')
        service = User.objects.get(username='svc-ci-jenkins')
        self.assertFalse(service.is_staff)
        self.assertFalse(service.has_usable_password())  # 服务账号不能从登录页进来

    def test_empty_whitelist_rejected(self):
        """本期核心约定：留空必须显式勾选「不限制来源 IP」。"""
        r = self.client.post(self.URL, self._payload(ip_whitelist=''), format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('白名单', r.json()['msg'])

    def test_unrestricted_requires_explicit_flag(self):
        r = self.client.post(self.URL,
                             self._payload(ip_whitelist='', ip_unrestricted=True),
                             format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(ApiCredential.objects.get(pk=r.json()['data']['id']).ip_unrestricted)

    def test_invalid_cidr_rejected(self):
        r = self.client.post(self.URL, self._payload(ip_whitelist='10.0.0.0/33'),
                             format='json')
        self.assertEqual(r.status_code, 400)

    def test_unknown_scope_rejected(self):
        r = self.client.post(self.URL, self._payload(scopes=['what:ever']), format='json')
        self.assertEqual(r.status_code, 400)

    def test_empty_scope_rejected(self):
        r = self.client.post(self.URL, self._payload(scopes=[]), format='json')
        self.assertEqual(r.status_code, 400)

    def test_signature_mode_requires_secret(self):
        r = self.client.post(self.URL, self._payload(
            sig_mode='hmac_sha256', sig_header='X-Hub-Signature-256'), format='json')
        self.assertEqual(r.status_code, 400)

    def test_signing_secret_never_returned(self):
        r = self.client.post(self.URL, self._payload(
            sig_mode='hmac_sha256', sig_header='X-Hub-Signature-256',
            signing_secret='a-long-enough-secret'), format='json')
        self.assertEqual(r.status_code, 200, r.content)
        data = r.json()['data']
        self.assertNotIn('signing_secret', data)
        self.assertTrue(data['has_signing_secret'])

    def test_bad_rate_limit_rejected(self):
        r = self.client.post(self.URL, self._payload(rate_limit='60 per minute'),
                             format='json')
        self.assertEqual(r.status_code, 400)

    def test_past_expiry_rejected(self):
        r = self.client.post(self.URL, self._payload(
            expires_at=(timezone.now() - timedelta(days=1)).isoformat()), format='json')
        self.assertEqual(r.status_code, 400)

    # ── 动作 ──────────────────────────────────────────────
    def test_rotate_and_toggle(self):
        credential_id = self.client.post(self.URL, self._payload(),
                                         format='json').json()['data']['id']
        r = self.client.post(f'{self.URL}{credential_id}/rotate_key/')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()['data']['api_key'].startswith('ak_'))

        r = self.client.post(f'{self.URL}{credential_id}/toggle_enabled/')
        self.assertFalse(r.json()['data']['enabled'])

    def test_scopes_endpoint(self):
        r = self.client.get(f'{self.URL}scopes/')
        self.assertEqual(r.status_code, 200)
        self.assertIn('artifact:report', [item['value'] for item in r.json()['data']])

    # ── 权限 ──────────────────────────────────────────────
    def test_non_admin_forbidden(self):
        member = User.objects.create(username='cred_member', is_staff=False)
        member.set_password(PASSWORD)
        member.save()
        client = self._login('cred_member')
        self.assertEqual(client.get(self.URL).status_code, 403)
        self.assertEqual(client.post(self.URL, self._payload(), format='json').status_code, 403)

    def test_write_requires_sudo(self):
        client = self._login('cred_admin')  # 新会话，没开 sudo 窗口
        r = client.post(self.URL, self._payload(), format='json')
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()['error_code'], 'sudo_required')


@override_settings(ROOT_URLCONF='ops_manager.urls', TRUSTED_PROXIES=TRUSTED)
class CredentialDimScopeTests(TestCase):
    """凭证数据范围：作用域管「能调哪类接口」，范围管「能作用在哪些资源上」。"""

    ALLOWED_IP = '203.0.113.9'

    def setUp(self):
        from apps.asset_mgr.models import Channel, Environment, ProjectGroup, Server

        cache.clear()
        self.pg_a = ProjectGroup.objects.create(name='项目A', code='ds-a')
        self.pg_b = ProjectGroup.objects.create(name='项目B', code='ds-b')
        self.ch = Channel.objects.create(name='官服', code='ds-official')
        self.env_test = Environment.objects.create(name='测试', code='ds-test')
        self.env_prod = Environment.objects.create(name='生产', code='ds-prod', is_prod=True)
        self.host_a = Server.objects.create(
            name='ds-host-a', private_ip='10.0.0.1',
            project_group=self.pg_a, channel=self.ch, environment=self.env_test)
        self.host_b = Server.objects.create(
            name='ds-host-b', private_ip='10.0.0.2',
            project_group=self.pg_b, channel=self.ch, environment=self.env_test)
        self.client = APIClient()

    def tearDown(self):
        cache.clear()

    def _credential(self, name, scopes, groups=(), envs=()):
        credential, raw = make_credential(name=name, scopes=scopes)
        if groups:
            credential.allowed_project_groups.set(groups)
        if envs:
            credential.allowed_environments.set(envs)
        return credential, raw

    def _get(self, url, key):
        return self.client.get(url, HTTP_X_API_KEY=key, REMOTE_ADDR=self.ALLOWED_IP)

    # ── 主机清单 ──────────────────────────────────────────
    def test_asset_read_narrows_to_allowed_groups(self):
        """收窄可见集而不是拒绝：忘了带筛选参数时拿到的是「我的全部」。"""
        _, key = self._credential('ds-deploy', ['asset:read'], groups=[self.pg_a])
        r = self._get('/api/assets/servers/', key)
        self.assertEqual(r.status_code, 200, r.content)
        names = [item['name'] for item in r.json()['data']['results']]
        self.assertEqual(names, ['ds-host-a'])

    def test_asset_read_without_scope_denied(self):
        """凭证绑的是真实服务账号，若权限类用「或」语义，这里会漏放行。"""
        _, key = self._credential('ds-noscope', ['artifact:report'])
        r = self._get('/api/assets/servers/', key)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()['error_code'], 'api_key_scope_denied')

    def test_asset_read_is_read_only(self):
        """一个「读」作用域绝不能顺带放行写——凭证认证绕过了 MFA 与 sudo。"""
        _, key = self._credential('ds-ro', ['asset:read'])
        r = self.client.post('/api/assets/servers/', {'name': 'x', 'private_ip': '10.0.0.9'},
                             format='json', HTTP_X_API_KEY=key, REMOTE_ADDR=self.ALLOWED_IP)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()['error_code'], 'api_key_scope_denied')

    def test_no_dim_scope_sees_everything(self):
        _, key = self._credential('ds-all', ['asset:read'])
        r = self._get('/api/assets/servers/', key)
        self.assertEqual(r.json()['data']['total'], 2)

    # ── 变量解析 ──────────────────────────────────────────
    def _resolved_url(self, pg, env):
        return (f'/api/exec/variables/resolved/?project_group={pg.code}'
                f'&channel={self.ch.code}&env={env.code}')

    def test_variable_read_within_scope(self):
        ExecVariable.objects.create(
            key='app_port', value='8080', project_group=self.pg_a,
            channel=self.ch, environment=self.env_test)
        _, key = self._credential('ds-vars', ['variable:read'], groups=[self.pg_a])
        r = self._get(self._resolved_url(self.pg_a, self.env_test), key)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()['data']['vars']['app_port'], 8080)

    def test_variable_read_outside_scope_denied(self):
        """三维度是必填参数，调用方已明确要哪一组，静默换成别的组更糟，所以拒绝。"""
        _, key = self._credential('ds-vars2', ['variable:read'], groups=[self.pg_a])
        r = self._get(self._resolved_url(self.pg_b, self.env_test), key)
        self.assertEqual(r.status_code, 403)
        self.assertIn('数据范围', r.json()['msg'])

    def test_secret_needs_extra_scope(self):
        """两级作用域：读变量与读密码的泄露后果不在一个量级。"""
        var = ExecVariable(
            key='db_password', is_secret=True, project_group=self.pg_a,
            channel=self.ch, environment=self.env_test)
        var.set_value('p@ss#word')
        var.save()

        _, plain_key = self._credential('ds-plain', ['variable:read'], groups=[self.pg_a])
        url = self._resolved_url(self.pg_a, self.env_test) + '&include_secrets=true'
        r = self._get(url, plain_key)
        self.assertEqual(r.status_code, 403)
        self.assertIn('variable:secret', r.json()['msg'])

        _, secret_key = self._credential(
            'ds-secret', ['variable:read', 'variable:secret'], groups=[self.pg_a])
        r = self._get(url, secret_key)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()['data']['vars']['db_password'], 'p@ss#word')

    def test_secret_excluded_without_flag(self):
        var = ExecVariable(
            key='db_password', is_secret=True, project_group=self.pg_a,
            channel=self.ch, environment=self.env_test)
        var.set_value('p@ss#word')
        var.save()
        _, key = self._credential(
            'ds-secret2', ['variable:read', 'variable:secret'], groups=[self.pg_a])
        r = self._get(self._resolved_url(self.pg_a, self.env_test), key)
        self.assertNotIn('db_password', r.json()['data']['vars'])
        self.assertEqual(r.json()['data']['secret_keys_excluded'], ['db_password'])

    # ── 高敏作用域禁止不限 IP ──────────────────────────────
    def test_high_risk_scope_forbids_unrestricted_ip(self):
        admin = User.objects.create(username='dim_admin', is_staff=True)
        admin.set_password(PASSWORD)
        admin.save()
        client = APIClient()
        r = client.post('/api/auth/login/',
                        {'username': 'dim_admin', 'password': PASSWORD}, format='json')
        client.credentials(HTTP_AUTHORIZATION='Bearer ' + r.json()['access'])
        grant_sudo(client, PASSWORD)

        r = client.post('/api/auth/api-credentials/', {
            'name': 'ds-risky', 'scopes': ['variable:secret'],
            'ip_unrestricted': True, 'ip_whitelist': '',
        }, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('高敏', r.json()['msg'])

        r = client.post('/api/auth/api-credentials/', {
            'name': 'ds-ok', 'scopes': ['variable:secret'],
            'ip_whitelist': '10.0.1.0/24',
            'allowed_project_groups': [self.pg_a.id],
        }, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()['data']['scope_label'], f'项目组 [{self.pg_a.id}]')


class RoleMatrixTests(TestCase):
    """角色矩阵：档位映射、无角色回退、能力位、内置角色保护。"""

    def setUp(self):
        cache.clear()
        self.super_role = Role.objects.get(code='super')
        self.ops_admin = Role.objects.get(code='ops_admin')
        self.ops_member = Role.objects.get(code='ops_member')

    def tearDown(self):
        cache.clear()

    def _user(self, name, role=None, is_staff=False):
        user = User.objects.create(username=name, is_staff=is_staff, role=role)
        user.set_password(PASSWORD)
        user.save()
        return user

    def _client(self, user):
        client = APIClient()
        r = client.post('/api/auth/login/',
                        {'username': user.username, 'password': PASSWORD}, format='json')
        client.credentials(HTTP_AUTHORIZATION='Bearer ' + r.json()['access'])
        return client

    # ── 档位映射 ──────────────────────────────────────────
    def test_method_level_mapping(self):
        from apps.auth_mgr import roles as roles_mod

        self.assertEqual(roles_mod.required_level('GET'), 'read')
        self.assertEqual(roles_mod.required_level('POST'), 'read_create')
        self.assertEqual(roles_mod.required_level('PATCH'), 'write')
        # 未知方法当 write（fail-closed）
        self.assertEqual(roles_mod.required_level('FROBNICATE'), 'write')

    def test_read_create_level_allows_post_not_patch(self):
        """保留 read_create 这一档：CMDB 现在就是「可查可增不可改删」。"""
        member = self._user('rm_member', role=self.ops_member)
        client = self._client(member)
        self.assertEqual(client.get('/api/assets/servers/').status_code, 200)
        r = client.post('/api/assets/servers/',
                        {'name': 'rm-host', 'private_ip': '10.7.7.7'}, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        server_id = r.json()['id']
        self.assertEqual(client.patch(f'/api/assets/servers/{server_id}/',
                                      {'os': 'ubuntu'}, format='json').status_code, 403)

    def test_none_level_hides_module(self):
        """普通运维对连接凭据是 none：连读都不给（凭据的读本身就泄露信息）。"""
        member = self._user('rm_member2', role=self.ops_member)
        self.assertEqual(self._client(member).get('/api/exec/credentials/').status_code, 403)

    def test_ops_admin_reads_credentials_but_cannot_write(self):
        """这正是二档模型缺的那一层：能干日常运维，但拿不到密钥与系统管理。"""
        admin = self._user('rm_opsadmin', role=self.ops_admin)
        client = self._client(admin)
        self.assertEqual(client.get('/api/exec/credentials/').status_code, 200)
        r = client.post('/api/exec/credentials/',
                        {'name': 'c1', 'username': 'root', 'auth_type': 'password',
                         'password': 'x'}, format='json')
        self.assertEqual(r.status_code, 403)
        # 系统管理不可见
        self.assertEqual(client.get('/api/auth/users/').status_code, 403)
        self.assertEqual(client.get('/api/system/configs/').status_code, 403)

    def test_super_role_full_access(self):
        boss = self._user('rm_super', role=self.super_role, is_staff=True)
        client = self._client(boss)
        grant_sudo(client, PASSWORD)
        self.assertEqual(client.get('/api/auth/users/').status_code, 200)
        self.assertEqual(client.get('/api/exec/credentials/').status_code, 200)

    # ── 无角色回退 ────────────────────────────────────────
    def test_no_role_staff_behaves_as_before(self):
        """存量账号不必一次性配角色：staff 仍是全权。"""
        legacy = self._user('rm_legacy_staff', role=None, is_staff=True)
        client = self._client(legacy)
        grant_sudo(client, PASSWORD)
        self.assertEqual(client.get('/api/auth/users/').status_code, 200)

    def test_no_role_member_behaves_as_before(self):
        legacy = self._user('rm_legacy_member', role=None, is_staff=False)
        client = self._client(legacy)
        self.assertEqual(client.get('/api/assets/servers/').status_code, 200)
        self.assertEqual(client.get('/api/exec/credentials/').status_code, 403)

    # ── 权限快照 ──────────────────────────────────────────
    def test_me_exposes_permission_snapshot(self):
        member = self._user('rm_snapshot', role=self.ops_member)
        data = self._client(member).get('/api/auth/me/').json()['data']
        self.assertEqual(data['permissions']['role'], 'ops_member')
        self.assertEqual(data['permissions']['modules']['credential'], 'none')
        self.assertFalse(data['permissions']['can_exec_prod'])

    def test_jwt_carries_permission_snapshot(self):
        from rest_framework_simplejwt.tokens import AccessToken

        member = self._user('rm_jwt', role=self.ops_member)
        client = APIClient()
        r = client.post('/api/auth/login/',
                        {'username': 'rm_jwt', 'password': PASSWORD}, format='json')
        payload = AccessToken(r.json()['access']).payload
        self.assertEqual(payload['perms']['role'], 'ops_member')

    # ── 内置角色保护 ──────────────────────────────────────
    def test_builtin_super_cannot_be_weakened(self):
        """一次手滑把超级管理降档，可能把所有人锁在系统之外。"""
        boss = self._user('rm_boss', role=self.super_role, is_staff=True)
        client = self._client(boss)
        grant_sudo(client, PASSWORD)
        weakened = dict(self.super_role.permissions, system='read')
        r = client.patch(f'/api/auth/roles/{self.super_role.id}/',
                         {'permissions': weakened}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('必须对所有模块保持读写', r.json()['msg'])

    def test_builtin_role_cannot_be_deleted(self):
        boss = self._user('rm_boss2', role=self.super_role, is_staff=True)
        client = self._client(boss)
        grant_sudo(client, PASSWORD)
        r = client.delete(f'/api/auth/roles/{self.ops_member.id}/')
        self.assertEqual(r.status_code, 400)
        self.assertIn('不可删除', r.json()['msg'])

    def test_role_in_use_cannot_be_deleted(self):
        boss = self._user('rm_boss3', role=self.super_role, is_staff=True)
        client = self._client(boss)
        grant_sudo(client, PASSWORD)
        r = client.post('/api/auth/roles/', {
            'name': '发布员', 'code': 'releaser',
            'permissions': {'exec': 'read', 'artifact': 'read'},
            'can_execute': True, 'can_exec_prod': False,
        }, format='json')
        self.assertEqual(r.status_code, 201, r.content)
        role_id = r.json()['id']
        # 未配置的模块补 none，避免「没配就继承了什么」的歧义
        self.assertEqual(r.json()['permissions']['system'], 'none')

        self._user('rm_releaser', role=Role.objects.get(pk=role_id))
        r = client.delete(f'/api/auth/roles/{role_id}/')
        self.assertEqual(r.status_code, 400)
        self.assertIn('仍有 1 个用户在用', r.json()['msg'])

    def test_unknown_module_or_level_rejected(self):
        boss = self._user('rm_boss4', role=self.super_role, is_staff=True)
        client = self._client(boss)
        grant_sudo(client, PASSWORD)
        for permissions in ({'nope': 'read'}, {'exec': 'superuser'}):
            r = client.post('/api/auth/roles/', {
                'name': f'bad-{list(permissions)[0]}', 'code': f'bad_{list(permissions)[0]}',
                'permissions': permissions,
            }, format='json')
            self.assertEqual(r.status_code, 400, permissions)

    def test_role_meta_endpoint(self):
        boss = self._user('rm_boss5', role=self.super_role, is_staff=True)
        client = self._client(boss)
        data = client.get('/api/auth/roles/meta/').json()['data']
        self.assertIn('cmdb', [m['value'] for m in data['modules']])
        self.assertIn('read_create', [level['value'] for level in data['levels']])


class ExecCapabilityTests(TestCase):
    """执行能力位与生产限制：能跑不能改、不能碰生产、范围外的任务碰不到。"""

    def setUp(self):
        from apps.asset_mgr.models import (
            Channel, Environment, ProjectGroup, Server, ServerService, ServiceType,
        )
        from apps.task_exec.models import ExecutionTask, TaskStep

        cache.clear()
        self.member_role = Role.objects.get(code='ops_member')
        self.pg = ProjectGroup.objects.create(name='项目A', code='ec-a')
        self.pg_other = ProjectGroup.objects.create(name='项目B', code='ec-b')
        self.ch = Channel.objects.create(name='官服', code='ec-official')
        self.env_test = Environment.objects.create(name='测试', code='ec-test')
        self.env_prod = Environment.objects.create(name='生产', code='ec-prod', is_prod=True)
        service = ServiceType.objects.create(name='基础', code='ec-base')
        server = Server.objects.create(
            name='ec-host', private_ip='10.5.5.5',
            project_group=self.pg, channel=self.ch, environment=self.env_test)
        ServerService.objects.create(server=server, service_type=service, status='planned')

        def make_task(name, env, pg=None):
            task = ExecutionTask.objects.create(
                name=name, project_group=pg or self.pg, channel=self.ch, environment=env)
            TaskStep.objects.create(task=task, order=1, name='m1', task_type='command',
                                    command='echo hi', service_type=service)
            return task

        self.task_test = make_task('测试任务', self.env_test)
        self.task_prod = make_task('生产任务', self.env_prod)
        self.task_other = make_task('别的项目', self.env_test, pg=self.pg_other)

    def tearDown(self):
        cache.clear()

    def _client(self, username, role=None, groups=()):
        user = User.objects.create(username=username, is_staff=False, role=role)
        user.set_password(PASSWORD)
        user.save()
        if groups:
            user.allowed_project_groups.set(groups)
        client = APIClient()
        r = client.post('/api/auth/login/',
                        {'username': username, 'password': PASSWORD}, format='json')
        client.credentials(HTTP_AUTHORIZATION='Bearer ' + r.json()['access'])
        return client

    def test_member_can_execute_but_not_edit(self):
        """「能跑不能改」：execute 是 POST，但不该按 read_create 档拦。"""
        client = self._client('ec_member', role=self.member_role)
        with mock.patch('apps.task_exec.trigger.dispatch', return_value=mock.Mock(id=1)):
            r = client.post(f'/api/exec/tasks/{self.task_test.id}/execute/', {}, format='json')
        self.assertEqual(r.status_code, 200, r.content)

        r = client.patch(f'/api/exec/tasks/{self.task_test.id}/',
                         {'name': '改个名'}, format='json')
        self.assertEqual(r.status_code, 403)

    def test_member_cannot_execute_prod(self):
        """最高频的真实诉求：不能碰生产。错了就是已经推上去了。"""
        client = self._client('ec_member2', role=self.member_role)
        r = client.post(f'/api/exec/tasks/{self.task_prod.id}/execute/', {}, format='json')
        self.assertEqual(r.status_code, 403)
        self.assertIn('不允许对生产环境', r.json()['msg'])

    def test_role_without_execute_capability(self):
        role = Role.objects.create(
            name='只读观察', code='viewer',
            permissions={'exec': 'read'}, can_execute=False, can_exec_prod=False)
        client = self._client('ec_viewer', role=role)
        r = client.post(f'/api/exec/tasks/{self.task_test.id}/execute/', {}, format='json')
        self.assertEqual(r.status_code, 403)
        self.assertIn('没有发起任务执行的权限', r.json()['msg'])

    def test_data_scope_blocks_other_project(self):
        client = self._client('ec_scoped', role=self.member_role, groups=[self.pg])
        with mock.patch('apps.task_exec.trigger.dispatch', return_value=mock.Mock(id=2)):
            r = client.post(f'/api/exec/tasks/{self.task_test.id}/execute/', {}, format='json')
        self.assertEqual(r.status_code, 200, r.content)

        r = client.post(f'/api/exec/tasks/{self.task_other.id}/execute/', {}, format='json')
        self.assertEqual(r.status_code, 403)
        self.assertIn('数据范围', r.json()['msg'])

    def test_confirm_uses_same_gate(self):
        """只在 execute 上拦等于留后门：continue/retry/skip 都会让剩余模块跑起来。"""
        from apps.task_exec.models import ExecutionTask

        ExecutionTask.objects.filter(pk=self.task_prod.pk).update(
            status='paused', pause_type='confirm')
        client = self._client('ec_member3', role=self.member_role)
        r = client.post(f'/api/exec/tasks/{self.task_prod.id}/confirm/',
                        {'action': 'continue'}, format='json')
        self.assertEqual(r.status_code, 403)
        self.assertIn('不允许对生产环境', r.json()['msg'])
