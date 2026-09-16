"""im_mgr 接口测试：飞书应用配置（单例/秘密不回显/启用前置校验）、
回调标识轮换、会话白名单 CRUD 与权限。"""
import base64
import hashlib
import json
import time
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from rest_framework.test import APIClient

from common.testing import grant_sudo

from apps.im_mgr import client, crypto
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


# ══════════════════════════════════════════════════════════
# T2：回调验签/解密 与 出站客户端
# ══════════════════════════════════════════════════════════

def _encrypt(encrypt_key, plaintext):
    """测试侧的加密（飞书发过来的密文是这么造的）：AES-256-CBC + PKCS#7。"""
    import os

    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = hashlib.sha256(encrypt_key.encode('utf-8')).digest()
    iv = os.urandom(16)
    raw = plaintext.encode('utf-8')
    pad = 16 - len(raw) % 16
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    body = encryptor.update(raw + bytes([pad]) * pad) + encryptor.finalize()
    return base64.b64encode(iv + body).decode('ascii')


def _callback_body(encrypt_key, payload):
    """造一份飞书回调的原始请求体字节。"""
    inner = json.dumps(payload, ensure_ascii=False)
    return json.dumps({'encrypt': _encrypt(encrypt_key, inner)}).encode('utf-8')


def _sign(encrypt_key, timestamp, nonce, body):
    return hashlib.sha256(
        f'{timestamp}{nonce}{encrypt_key}'.encode('utf-8') + body).hexdigest()


class FeishuCryptoTests(TestCase):
    ENCRYPT_KEY = 'encrypt-value'
    TOKEN = 'token-value'

    def setUp(self):
        cache.clear()
        self.app = FeishuApp.load()
        for key, value in FULL_CONFIG.items():
            setattr(self.app, key, value)
        self.app.enabled = True
        self.app.save()

    def _event(self, **extra):
        payload = {'schema': '2.0',
                   'header': {'token': self.TOKEN, 'event_type': 'im.message.receive_v1'},
                   'event': {'message': {'chat_id': 'oc_1'}}}
        payload.update(extra)
        return payload

    def test_decrypt_roundtrip(self):
        cipher = _encrypt(self.ENCRYPT_KEY, '你好 feishu')
        self.assertEqual(crypto.decrypt(self.ENCRYPT_KEY, cipher), '你好 feishu')

    def test_decrypt_wrong_key(self):
        cipher = _encrypt(self.ENCRYPT_KEY, 'payload')
        with self.assertRaises(crypto.CallbackError):
            crypto.decrypt('another-key', cipher)

    def test_decrypt_bad_input(self):
        with self.assertRaises(crypto.CallbackError):
            crypto.decrypt(self.ENCRYPT_KEY, '不是base64!!')
        with self.assertRaises(crypto.CallbackError):
            crypto.decrypt(self.ENCRYPT_KEY, '')

    def test_verify_signature(self):
        body = b'{"encrypt":"x"}'
        sig = _sign(self.ENCRYPT_KEY, '1700000000', 'n1', body)
        crypto.verify_signature(self.ENCRYPT_KEY, '1700000000', 'n1', body, sig)
        with self.assertRaises(crypto.CallbackError):
            crypto.verify_signature(self.ENCRYPT_KEY, '1700000000', 'n1', body, 'deadbeef')
        with self.assertRaises(crypto.CallbackError):
            crypto.verify_signature(self.ENCRYPT_KEY, '1700000000', 'n1', body, '')

    def test_signature_is_over_raw_bytes(self):
        """重新序列化过的请求体验不过签——这正是必须留住原始 bytes 的原因。"""
        body = b'{"encrypt":"x"}'
        sig = _sign(self.ENCRYPT_KEY, '1700000000', 'n1', body)
        reserialized = json.dumps(json.loads(body)).encode('utf-8')
        self.assertNotEqual(body, reserialized)
        with self.assertRaises(crypto.CallbackError):
            crypto.verify_signature(
                self.ENCRYPT_KEY, '1700000000', 'n1', reserialized, sig)

    def test_verify_timestamp_window(self):
        now = 1700000000
        crypto.verify_timestamp(str(now - 10), now=now)
        with self.assertRaises(crypto.CallbackError):
            crypto.verify_timestamp(str(now - crypto.TIMESTAMP_WINDOW - 1), now=now)
        with self.assertRaises(crypto.CallbackError):
            crypto.verify_timestamp(str(now + crypto.TIMESTAMP_WINDOW + 1), now=now)
        with self.assertRaises(crypto.CallbackError):
            crypto.verify_timestamp('not-a-number', now=now)

    def test_consume_nonce_rejects_replay(self):
        crypto.consume_nonce('nonce-1')
        with self.assertRaises(crypto.CallbackError):
            crypto.consume_nonce('nonce-1')
        crypto.consume_nonce('nonce-2')

    def test_consume_nonce_fail_closed_when_cache_down(self):
        with patch('apps.im_mgr.dedupe.cache.add', side_effect=RuntimeError('redis down')):
            with self.assertRaises(crypto.CallbackError):
                crypto.consume_nonce('nonce-3')

    def test_verify_callback_ok(self):
        body = _callback_body(self.ENCRYPT_KEY, self._event())
        ts, nonce = str(int(time.time())), 'n-ok'
        payload = crypto.verify_callback(
            self.app, ts, nonce, _sign(self.ENCRYPT_KEY, ts, nonce, body), body)
        self.assertEqual(payload['header']['event_type'], 'im.message.receive_v1')

    def test_verify_callback_rejects_token_mismatch(self):
        event = self._event()
        event['header']['token'] = 'wrong-token'
        body = _callback_body(self.ENCRYPT_KEY, event)
        ts, nonce = str(int(time.time())), 'n-token'
        with self.assertRaises(crypto.CallbackError):
            crypto.verify_callback(
                self.app, ts, nonce, _sign(self.ENCRYPT_KEY, ts, nonce, body), body)

    def test_verify_callback_rejects_replay(self):
        body = _callback_body(self.ENCRYPT_KEY, self._event())
        ts, nonce = str(int(time.time())), 'n-replay'
        sig = _sign(self.ENCRYPT_KEY, ts, nonce, body)
        crypto.verify_callback(self.app, ts, nonce, sig, body)
        with self.assertRaises(crypto.CallbackError):
            crypto.verify_callback(self.app, ts, nonce, sig, body)

    def test_payload_token_accepts_both_shapes(self):
        self.assertEqual(crypto.payload_token({'header': {'token': 'a'}}), 'a')
        self.assertEqual(crypto.payload_token({'token': 'b'}), 'b')
        self.assertEqual(crypto.payload_token({}), '')


class FakeResponse:
    def __init__(self, body, status_code=200, text=''):
        self._body = body
        self.status_code = status_code
        self.text = text or str(body)

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeSession:
    """记录请求并按队列返回响应；队列耗尽后重复最后一条。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.trust_env = True

    def request(self, method, url, headers=None, json=None, params=None, timeout=None):
        self.calls.append({'method': method, 'url': url, 'headers': headers or {},
                           'json': json, 'params': params})
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, FakeResponse):
            return item
        if isinstance(item, Exception):
            raise item  # 网络层异常
        return FakeResponse(item)


TOKEN_OK = {'code': 0, 'msg': 'ok', 'tenant_access_token': 't-1', 'expire': 7200}
TOKEN_OK2 = {'code': 0, 'msg': 'ok', 'tenant_access_token': 't-2', 'expire': 7200}


class FeishuClientTests(TestCase):
    def setUp(self):
        cache.clear()
        app = FeishuApp.load()
        for key, value in FULL_CONFIG.items():
            setattr(app, key, value)
        app.enabled = True
        app.save()

    def _run(self, responses, fn):
        session = FakeSession(responses)
        with patch('apps.im_mgr.client.requests.Session', return_value=session):
            return fn(), session

    def test_token_cached_across_calls(self):
        def run():
            first = client.get_tenant_token()
            second = client.get_tenant_token()
            return first, second

        (first, second), session = self._run([TOKEN_OK], run)
        self.assertEqual(first, 't-1')
        self.assertEqual(second, 't-1')
        # 只打了一次取 token 的接口
        self.assertEqual(len(session.calls), 1)

    def test_token_error_raises(self):
        with self.assertRaises(client.FeishuError):
            self._run([{'code': 10003, 'msg': 'app not found'}],
                      client.get_tenant_token)

    def test_disabled_app_raises(self):
        FeishuApp.objects.filter(pk=FeishuApp.SINGLETON_PK).update(enabled=False)
        with self.assertRaises(client.FeishuError) as ctx:
            client.get_tenant_token()
        self.assertIn('飞书接入不可用', str(ctx.exception))

    def test_send_card_returns_message_id(self):
        responses = [TOKEN_OK, {'code': 0, 'data': {'message_id': 'om_1'}}]
        message_id, session = self._run(
            responses, lambda: client.send_card('oc_1', {'config': {}}))
        self.assertEqual(message_id, 'om_1')
        sent = session.calls[-1]
        self.assertEqual(sent['method'], 'POST')
        self.assertEqual(sent['params'], {'receive_id_type': 'chat_id'})
        self.assertEqual(sent['headers']['Authorization'], 'Bearer t-1')
        # content 必须是 JSON 字符串而不是对象
        self.assertIsInstance(sent['json']['content'], str)

    def test_update_card_uses_patch(self):
        responses = [TOKEN_OK, {'code': 0, 'data': {}}]
        _, session = self._run(
            responses, lambda: client.update_card('om_1', {'config': {}}))
        sent = session.calls[-1]
        self.assertEqual(sent['method'], 'PATCH')
        self.assertTrue(sent['url'].endswith('/open-apis/im/v1/messages/om_1'))

    def test_invalid_token_refreshes_once(self):
        responses = [
            TOKEN_OK,
            {'code': 99991663, 'msg': 'invalid access token'},
            TOKEN_OK2,
            {'code': 0, 'data': {'message_id': 'om_2'}},
        ]
        message_id, session = self._run(
            responses, lambda: client.send_card('oc_1', {'config': {}}))
        self.assertEqual(message_id, 'om_2')
        self.assertEqual(len(session.calls), 4)
        self.assertEqual(session.calls[-1]['headers']['Authorization'], 'Bearer t-2')

    def test_business_error_raises_without_retry(self):
        responses = [TOKEN_OK, {'code': 230001, 'msg': 'bot not in chat'}]
        with self.assertRaises(client.FeishuError) as ctx:
            self._run(responses, lambda: client.send_card('oc_1', {'config': {}}))
        self.assertEqual(ctx.exception.code, 230001)

    def test_network_error_wrapped(self):
        import requests as _requests

        with self.assertRaises(client.FeishuError):
            self._run([_requests.ConnectionError('boom')], client.get_tenant_token)

    def test_non_json_response_wrapped(self):
        session = FakeSession([FakeResponse(ValueError('no json'), text='<html>502</html>')])
        with patch('apps.im_mgr.client.requests.Session', return_value=session):
            with self.assertRaises(client.FeishuError):
                client.get_tenant_token()

    def test_get_user(self):
        responses = [TOKEN_OK, {'code': 0, 'data': {'user': {'union_id': 'on_1',
                                                             'email': 'a@b.com'}}}]
        user, session = self._run(responses, lambda: client.get_user('ou_1'))
        self.assertEqual(user['union_id'], 'on_1')
        self.assertEqual(session.calls[-1]['params'], {'user_id_type': 'open_id'})


# ══════════════════════════════════════════════════════════
# T3：回调入口（公网，免登录）
# ══════════════════════════════════════════════════════════

class FeishuCallbackViewTests(TestCase):
    ENCRYPT_KEY = 'encrypt-value'
    TOKEN = 'token-value'

    def setUp(self):
        cache.clear()
        self.app = FeishuApp.load()
        for key, value in FULL_CONFIG.items():
            setattr(self.app, key, value)
        # 测试客户端来自回环，白名单必须含它，否则每条回调都会被 IP 那道拦下
        self.app.ip_whitelist = '127.0.0.1'
        self.app.enabled = True
        self.app.save()
        self.client = APIClient()
        self.event_url = self.app.event_path
        self.card_url = self.app.card_path

    def _post(self, url, payload, *, nonce=None, timestamp=None,
              signature=None, encrypt_key=None, **extra):
        key = encrypt_key or self.ENCRYPT_KEY
        body = _callback_body(key, payload)
        ts = timestamp or str(int(time.time()))
        nonce = nonce or f'n-{time.time_ns()}'
        sig = signature or _sign(key, ts, nonce, body)
        return self.client.post(
            url, data=body, content_type='application/json',
            HTTP_X_LARK_REQUEST_TIMESTAMP=ts,
            HTTP_X_LARK_REQUEST_NONCE=nonce,
            HTTP_X_LARK_SIGNATURE=sig,
            **extra,
        )

    def _event(self, event_id='ev-1', event_type='im.message.receive_v1'):
        return {'schema': '2.0',
                'header': {'token': self.TOKEN, 'event_id': event_id,
                           'event_type': event_type},
                'event': {'message': {'chat_id': 'oc_1'}}}

    def _card(self, event_id='ca-1'):
        return {'schema': '2.0',
                'header': {'token': self.TOKEN, 'event_id': event_id,
                           'event_type': 'card.action.trigger'},
                'action': {'value': {'act': 'run', 'task': 1}}}

    # ── 正常路径 ──────────────────────────────────────────
    def test_url_verification_echoes_challenge(self):
        payload = {'type': 'url_verification', 'token': self.TOKEN,
                   'challenge': 'chal-123'}
        resp = self._post(self.event_url, payload)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['challenge'], 'chal-123')

    def test_event_dispatched(self):
        with patch('apps.im_mgr.handlers.handle_event', return_value=None) as handler:
            resp = self._post(self.event_url, self._event())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {'code': 0})
        handler.assert_called_once()

    def test_card_action_dispatched(self):
        with patch('apps.im_mgr.handlers.handle_card_action',
                   return_value={'toast': {'content': '已提交'}}) as handler:
            resp = self._post(self.card_url, self._card())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {'toast': {'content': '已提交'}})
        handler.assert_called_once()

    def test_card_default_ack_is_empty_object(self):
        with patch('apps.im_mgr.handlers.handle_card_action', return_value=None):
            resp = self._post(self.card_url, self._card())
        self.assertEqual(resp.json(), {})

    def test_callback_needs_no_login(self):
        """回调不走 JWT，也不该被 MFA 强制挡住（认证类置空）。"""
        resp = self._post(self.event_url, self._event())
        self.assertEqual(resp.status_code, 200)

    # ── 各道门 ────────────────────────────────────────────
    def test_bad_signature_rejected_silently(self):
        with patch('apps.im_mgr.handlers.handle_event') as handler:
            resp = self._post(self.event_url, self._event(), signature='deadbeef')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {'code': 0})
        handler.assert_not_called()

    def test_wrong_slug_rejected_silently(self):
        url = f'{FeishuApp.CALLBACK_PREFIX}{"0" * 32}/event/'
        with patch('apps.im_mgr.handlers.handle_event') as handler:
            resp = self._post(url, self._event())
        self.assertEqual(resp.status_code, 200)
        handler.assert_not_called()

    def test_malformed_slug_is_404(self):
        """形状不对的连路由都进不去（正则限定 32 位 hex）。"""
        resp = self.client.post(f'{FeishuApp.CALLBACK_PREFIX}short/event/',
                                data=b'{}', content_type='application/json')
        self.assertEqual(resp.status_code, 404)

    def test_ip_not_allowed_rejected(self):
        FeishuApp.objects.filter(pk=FeishuApp.SINGLETON_PK).update(
            ip_whitelist='203.0.113.0/24')
        with patch('apps.im_mgr.handlers.handle_event') as handler:
            resp = self._post(self.event_url, self._event())
        self.assertEqual(resp.status_code, 200)
        handler.assert_not_called()

    def test_spoofed_xff_does_not_bypass_ip_check(self):
        """伪造的 XFF 左段无效：可信代理链从右往左剥，右端才是 nginx 追加的真实来源。"""
        FeishuApp.objects.filter(pk=FeishuApp.SINGLETON_PK).update(
            ip_whitelist='203.0.113.5')
        with patch('apps.im_mgr.handlers.handle_event') as handler:
            resp = self._post(self.event_url, self._event(),
                              HTTP_X_FORWARDED_FOR='203.0.113.5, 198.51.100.9')
        self.assertEqual(resp.status_code, 200)
        handler.assert_not_called()

    def test_disabled_app_rejected(self):
        FeishuApp.objects.filter(pk=FeishuApp.SINGLETON_PK).update(enabled=False)
        with patch('apps.im_mgr.handlers.handle_event') as handler:
            resp = self._post(self.event_url, self._event())
        self.assertEqual(resp.status_code, 200)
        handler.assert_not_called()

    def test_token_mismatch_rejected(self):
        payload = self._event()
        payload['header']['token'] = 'wrong'
        with patch('apps.im_mgr.handlers.handle_event') as handler:
            resp = self._post(self.event_url, payload)
        self.assertEqual(resp.status_code, 200)
        handler.assert_not_called()

    def test_stale_timestamp_rejected(self):
        old = str(int(time.time()) - crypto.TIMESTAMP_WINDOW - 60)
        with patch('apps.im_mgr.handlers.handle_event') as handler:
            resp = self._post(self.event_url, self._event(), timestamp=old)
        self.assertEqual(resp.status_code, 200)
        handler.assert_not_called()

    # ── 幂等 ──────────────────────────────────────────────
    def test_duplicate_event_id_handled_once(self):
        """飞书投递重试换了 nonce 与签名，靠 event_id 判重。"""
        payload = self._event(event_id='ev-dup')
        with patch('apps.im_mgr.handlers.handle_event') as handler:
            first = self._post(self.event_url, payload)
            second = self._post(self.event_url, payload)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(handler.call_count, 1)

    def test_replayed_request_rejected_by_nonce(self):
        payload = self._event(event_id='ev-replay')
        body = _callback_body(self.ENCRYPT_KEY, payload)
        ts, nonce = str(int(time.time())), 'n-fixed'
        sig = _sign(self.ENCRYPT_KEY, ts, nonce, body)
        headers = {'HTTP_X_LARK_REQUEST_TIMESTAMP': ts,
                   'HTTP_X_LARK_REQUEST_NONCE': nonce,
                   'HTTP_X_LARK_SIGNATURE': sig}
        with patch('apps.im_mgr.handlers.handle_event') as handler:
            self.client.post(self.event_url, data=body,
                             content_type='application/json', **headers)
            self.client.post(self.event_url, data=body,
                             content_type='application/json', **headers)
        self.assertEqual(handler.call_count, 1)

    def test_dedupe_cache_down_is_fail_closed(self):
        with patch('apps.im_mgr.dedupe.cache.add', side_effect=RuntimeError('down')):
            with patch('apps.im_mgr.handlers.handle_event') as handler:
                resp = self._post(self.event_url, self._event())
        self.assertEqual(resp.status_code, 200)
        handler.assert_not_called()

    # ── 健壮性 ────────────────────────────────────────────
    def test_handler_exception_still_acks(self):
        """业务异常不能改变回包形态，否则错误差异就是一份探测反馈。"""
        with patch('apps.im_mgr.handlers.handle_event', side_effect=RuntimeError('boom')):
            resp = self._post(self.event_url, self._event())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {'code': 0})

    def test_callback_not_audited(self):
        """群里每条消息都是一次回调，落审计表只有噪声（见 audit/middleware.SKIP_PATHS）。"""
        from apps.audit.models import AuditLog

        self._post(self.event_url, self._event())
        self.assertFalse(AuditLog.objects.filter(path__startswith=
                                                 FeishuApp.CALLBACK_PREFIX).exists())
