"""im_mgr 接口测试：飞书应用配置（单例/秘密不回显/启用前置校验）、
回调标识轮换、会话白名单 CRUD 与权限。"""
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from rest_framework.test import APIClient

from common.testing import grant_sudo

from apps.im_mgr.models import FeishuApp, FeishuChannel

PASSWORD = 'Pass!2345xyz'
#: 配齐一份可启用的配置（IP 白名单用文档示例段，测试不依赖真实出口 IP）
FULL_CONFIG = {
    'app_id': 'cli_test0001',
    'app_secret': 'secret-value',
    'encrypt_key': 'encrypt-value',
    'verification_token': 'token-value',
    'ip_whitelist': '203.0.113.0/24',
}


class BaseApiTest(TestCase):
    def _client(self, is_staff=True, username=None, sudo=True):
        username = username or ('admin_im' if is_staff else 'user_im')
        user = get_user_model().objects.create(username=username, is_staff=is_staff)
        user.set_password(PASSWORD)
        user.save()
        client = APIClient()
        resp = client.post('/api/auth/login/',
                           {'username': username, 'password': PASSWORD}, format='json')
        client.credentials(HTTP_AUTHORIZATION='Bearer ' + resp.json()['access'])
        # 配置面写操作要求 sudo 二次验证（common/sudo.py）
        if is_staff and sudo:
            grant_sudo(client)
        return client


class FeishuAppModelTests(TestCase):
    def test_load_is_singleton(self):
        first = FeishuApp.load()
        second = FeishuApp.load()
        self.assertEqual(first.pk, FeishuApp.SINGLETON_PK)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(FeishuApp.objects.count(), 1)

    def test_save_forces_singleton_pk(self):
        FeishuApp.load()
        another = FeishuApp(app_id='cli_x')
        another.save()
        self.assertEqual(FeishuApp.objects.count(), 1)
        self.assertEqual(FeishuApp.load().app_id, 'cli_x')

    def test_save_keeps_callback_slug_and_created_at(self):
        """绕过 load() 直接保存，不得静默轮换回调地址、不得抹掉创建时间。"""
        app = FeishuApp.load()
        slug, created = app.callback_slug, app.created_at
        FeishuApp(app_id='cli_y').save()
        reloaded = FeishuApp.load()
        self.assertEqual(reloaded.callback_slug, slug)
        self.assertEqual(reloaded.created_at, created)

    def test_delete_rejected(self):
        app = FeishuApp.load()
        with self.assertRaises(RuntimeError):
            app.delete()

    def test_secret_stored_encrypted(self):
        """库里必须是密文，取出来是明文（EncryptedTextField 透明加解密）。"""
        app = FeishuApp.load()
        app.app_secret = 'plain-secret'
        app.save()
        # 必须绕开 ORM 取原始列值：走 values_list 会经 from_db_value 解密回明文
        with connection.cursor() as cursor:
            cursor.execute('SELECT app_secret FROM im_feishu_app WHERE id = %s', [app.pk])
            raw = cursor.fetchone()[0]
        self.assertNotEqual(raw, 'plain-secret')
        self.assertEqual(FeishuApp.load().app_secret, 'plain-secret')

    def test_missing_fields_and_ready(self):
        app = FeishuApp.load()
        self.assertTrue(app.missing_fields())
        self.assertFalse(app.is_ready())
        for key, value in FULL_CONFIG.items():
            setattr(app, key, value)
        app.enabled = True
        app.save()
        self.assertEqual(app.missing_fields(), [])
        self.assertTrue(app.is_ready())

    def test_ip_allowed_fail_closed_when_blank(self):
        """白名单留空且未显式勾「不限制」→ 一律拒（与 API 凭证同口径）。"""
        app = FeishuApp.load()
        self.assertFalse(app.ip_allowed('203.0.113.5'))
        app.ip_whitelist = '203.0.113.0/24'
        self.assertTrue(app.ip_allowed('203.0.113.5'))
        self.assertFalse(app.ip_allowed('198.51.100.5'))
        app.ip_unrestricted = True
        self.assertTrue(app.ip_allowed('198.51.100.5'))

    def test_rotate_callback_slug(self):
        app = FeishuApp.load()
        before = app.callback_slug
        after = app.rotate_callback_slug()
        self.assertNotEqual(before, after)
        self.assertIn(after, app.event_path)
        self.assertIn(after, app.card_path)


class FeishuAppApiTests(BaseApiTest):
    def test_get_creates_and_hides_secrets(self):
        admin = self._client()
        resp = admin.get('/api/feishu/app/')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()['data']
        self.assertNotIn('app_secret', data)
        self.assertFalse(data['has_app_secret'])
        self.assertTrue(data['missing_fields'])
        self.assertIn(data['callback_slug'], data['event_url'])
        self.assertIn('/card/', data['card_url'])

    def test_put_saves_secret_without_echo(self):
        admin = self._client()
        resp = admin.put('/api/feishu/app/', FULL_CONFIG, format='json')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()['data']
        self.assertNotIn('app_secret', data)
        self.assertTrue(data['has_app_secret'])
        self.assertTrue(data['has_encrypt_key'])
        self.assertTrue(data['has_verification_token'])
        self.assertEqual(data['missing_fields'], [])
        self.assertEqual(FeishuApp.load().app_secret, 'secret-value')

    def test_put_blank_secret_keeps_old(self):
        admin = self._client()
        admin.put('/api/feishu/app/', FULL_CONFIG, format='json')
        resp = admin.put('/api/feishu/app/', {'app_secret': '', 'remark': '改备注'}, format='json')
        self.assertEqual(resp.status_code, 200)
        app = FeishuApp.load()
        self.assertEqual(app.app_secret, 'secret-value')
        self.assertEqual(app.remark, '改备注')

    def test_enable_requires_full_config(self):
        admin = self._client()
        resp = admin.put('/api/feishu/app/', {'app_id': 'cli_x', 'enabled': True}, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('App Secret', resp.json()['msg'])
        self.assertFalse(FeishuApp.load().enabled)

    def test_enable_requires_ip_policy(self):
        """白名单留空又不勾「不限制」，不允许启用——公网入口不能裸奔。"""
        admin = self._client()
        payload = {**FULL_CONFIG, 'ip_whitelist': '', 'enabled': True}
        resp = admin.put('/api/feishu/app/', payload, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('IP 白名单', resp.json()['msg'])

        payload = {**payload, 'ip_unrestricted': True}
        resp = admin.put('/api/feishu/app/', payload, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(FeishuApp.load().enabled)

    def test_enable_full_config(self):
        admin = self._client()
        resp = admin.put('/api/feishu/app/', {**FULL_CONFIG, 'enabled': True}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(FeishuApp.load().enabled)

    def test_invalid_ip_whitelist_rejected(self):
        admin = self._client()
        resp = admin.put('/api/feishu/app/', {'ip_whitelist': '不是IP'}, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('IP 白名单', resp.json()['msg'])

    def test_sso_requires_enabled(self):
        admin = self._client()
        resp = admin.put('/api/feishu/app/', {**FULL_CONFIG, 'sso_enabled': True}, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('飞书接入', resp.json()['msg'])

    def test_rotate_slug_changes_callback_url(self):
        admin = self._client()
        before = admin.get('/api/feishu/app/').json()['data']['callback_slug']
        resp = admin.post('/api/feishu/app/rotate-slug/', {}, format='json')
        self.assertEqual(resp.status_code, 200)
        after = resp.json()['data']['callback_slug']
        self.assertNotEqual(before, after)
        self.assertIn(after, resp.json()['data']['event_url'])

    def test_non_admin_forbidden(self):
        member = self._client(is_staff=False)
        self.assertEqual(member.get('/api/feishu/app/').status_code, 403)
        self.assertEqual(
            member.put('/api/feishu/app/', FULL_CONFIG, format='json').status_code, 403)

    def test_write_requires_sudo(self):
        admin = self._client(username='admin_nosudo', sudo=False)
        resp = admin.put('/api/feishu/app/', FULL_CONFIG, format='json')
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json().get('error_code'), 'sudo_required')
        # 读不受 sudo 限制
        self.assertEqual(admin.get('/api/feishu/app/').status_code, 200)


class FeishuChannelApiTests(BaseApiTest):
    def setUp(self):
        self.channel = FeishuChannel.objects.create(
            chat_id='oc_group_1', name='运维群', allow_execute=True)

    def test_create_and_list(self):
        admin = self._client()
        resp = admin.post('/api/feishu/channels/', {
            'chat_id': 'oc_group_2', 'name': '发布通知群', 'chat_type': 'group',
        }, format='json')
        self.assertEqual(resp.status_code, 201)
        listed = admin.get('/api/feishu/channels/?allow_execute=true').json()['data']
        self.assertEqual(listed['total'], 1)
        self.assertEqual(listed['results'][0]['chat_id'], 'oc_group_1')

    def test_duplicate_chat_id_rejected(self):
        admin = self._client()
        resp = admin.post('/api/feishu/channels/', {
            'chat_id': 'oc_group_1', 'name': '重复群',
        }, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_disabled_channel_cannot_allow_execute(self):
        admin = self._client()
        resp = admin.patch(f'/api/feishu/channels/{self.channel.id}/',
                           {'enabled': False}, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_executable_helper(self):
        self.assertTrue(FeishuChannel.executable('oc_group_1'))
        self.assertFalse(FeishuChannel.executable('oc_unknown'))
        FeishuChannel.objects.filter(pk=self.channel.pk).update(enabled=False)
        self.assertFalse(FeishuChannel.executable('oc_group_1'))

    def test_non_admin_forbidden(self):
        member = self._client(is_staff=False)
        self.assertEqual(member.get('/api/feishu/channels/').status_code, 403)
