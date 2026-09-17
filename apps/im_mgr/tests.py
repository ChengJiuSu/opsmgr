"""im_mgr 接口测试：飞书应用配置（单例/秘密不回显/启用前置校验）、
回调标识轮换、会话白名单 CRUD 与权限。"""
import base64
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from common.testing import grant_sudo

from apps.im_mgr import access, cards, client, crypto, handlers, notify, sso, tasks
from apps.im_mgr.models import (
    FeishuApp, FeishuBinding, FeishuCardMessage, FeishuChannel, FeishuConfirmToken,
)

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
        """key 不对一律抛 CallbackError。

        跑多轮是必要的：IV 随机，PKCS#7 去填充有一定概率碰巧通过，于是错误从
        「填充非法」变成解码失败——单跑一次抓不到，而漏掉它的后果是异常穿透
        视图、回包从恒定的 200 变成 500。
        """
        for _ in range(60):
            cipher = _encrypt(self.ENCRYPT_KEY, 'payload')
            with self.assertRaises(crypto.CallbackError):
                crypto.decrypt('another-key', cipher)

    def test_callback_stays_constant_on_unexpected_error(self):
        """校验链路抛任何异常，回包都必须还是那个恒定包。"""
        self.app.ip_whitelist = '127.0.0.1'
        self.app.save(update_fields=['ip_whitelist'])
        body = _callback_body(self.ENCRYPT_KEY, self._event())
        ts, nonce = str(int(time.time())), 'n-boom'
        with patch('apps.im_mgr.crypto.parse_body', side_effect=RuntimeError('boom')):
            resp = APIClient().post(
                self.app.event_path, data=body, content_type='application/json',
                HTTP_X_LARK_REQUEST_TIMESTAMP=ts,
                HTTP_X_LARK_REQUEST_NONCE=nonce,
                HTTP_X_LARK_SIGNATURE=_sign(self.ENCRYPT_KEY, ts, nonce, body))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {'code': 0})

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


# ══════════════════════════════════════════════════════════
# T4：飞书登录（SSO）与 MFA 口径
# ══════════════════════════════════════════════════════════

PROFILE = {
    'union_id': 'on_union_1',
    'open_id': 'ou_open_1',
    'name': '张三',
    'enterprise_email': 'zhangsan@corp.com',
}


class FeishuSsoTests(TestCase):
    def setUp(self):
        cache.clear()
        self.app = FeishuApp.load()
        for key, value in FULL_CONFIG.items():
            setattr(self.app, key, value)
        self.app.enabled = True
        self.app.sso_enabled = True
        self.app.sso_redirect_uri = 'https://ops.example.com/login/feishu'
        self.app.save()
        self.client = APIClient()

    # ── 授权入口 ──────────────────────────────────────────
    def test_status_reflects_switch(self):
        self.assertTrue(self.client.get('/api/feishu/sso/status/').json()['data']['enabled'])
        FeishuApp.objects.filter(pk=FeishuApp.SINGLETON_PK).update(sso_enabled=False)
        self.assertFalse(self.client.get('/api/feishu/sso/status/').json()['data']['enabled'])

    def test_authorize_returns_url_with_one_time_state(self):
        data = self.client.get('/api/feishu/sso/authorize/').json()['data']
        self.assertIn('app_id=cli_test0001', data['authorize_url'])
        self.assertIn('redirect_uri=https%3A%2F%2Fops.example.com%2Flogin%2Ffeishu',
                      data['authorize_url'])
        self.assertIn(f'state={data["state"]}', data['authorize_url'])
        # state 可用一次，用完即失效
        sso.consume_state(data['state'])
        with self.assertRaises(sso.SsoError):
            sso.consume_state(data['state'])

    def test_authorize_rejected_when_sso_off(self):
        FeishuApp.objects.filter(pk=FeishuApp.SINGLETON_PK).update(sso_enabled=False)
        resp = self.client.get('/api/feishu/sso/authorize/')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('飞书登录未启用', resp.json()['msg'])

    # ── 登录回调 ──────────────────────────────────────────
    def _login(self, profile=None, state=None):
        """走完整回调链路，只把飞书两次出站调用换成假数据。"""
        profile = PROFILE if profile is None else profile
        state = state or self.client.get('/api/feishu/sso/authorize/').json()['data']['state']

        def fake_call(method, path, **kwargs):
            if path == sso.OIDC_TOKEN_PATH:
                return {'access_token': 'u-token'}
            if path == sso.USER_INFO_PATH:
                return dict(profile)
            raise AssertionError(f'未预期的飞书调用：{path}')

        with patch('apps.im_mgr.client.get_app_token', return_value='a-token'), \
                patch('apps.im_mgr.client.call', side_effect=fake_call):
            return self.client.post('/api/feishu/sso/callback/',
                                    {'code': 'c-1', 'state': state}, format='json')

    def test_login_with_existing_binding(self):
        user = get_user_model().objects.create(username='zhangsan', is_staff=True)
        FeishuBinding.objects.create(user=user, union_id='on_union_1', open_id='ou_open_1')
        resp = self._login()
        self.assertEqual(resp.status_code, 200)
        self.assertIn('access', resp.json())
        binding = FeishuBinding.objects.get(union_id='on_union_1')
        self.assertIsNotNone(binding.last_login_at)

    def test_login_binds_by_email(self):
        user = get_user_model().objects.create(username='zs', email='ZhangSan@corp.com')
        resp = self._login()
        self.assertEqual(resp.status_code, 200)
        binding = FeishuBinding.objects.get(union_id='on_union_1')
        self.assertEqual(binding.user_id, user.id)
        self.assertEqual(binding.source, 'email')

    def test_ambiguous_email_not_auto_bound(self):
        """同一邮箱匹配到多个账号一律放弃——这条路最容易绑错人。"""
        get_user_model().objects.create(username='a1', email='zhangsan@corp.com')
        get_user_model().objects.create(username='a2', email='zhangsan@corp.com')
        resp = self._login()
        self.assertEqual(resp.status_code, 401)
        self.assertIn('未绑定平台用户', resp.json()['msg'])
        self.assertFalse(FeishuBinding.objects.exists())

    def test_unknown_user_rejected_by_default(self):
        resp = self._login()
        self.assertEqual(resp.status_code, 401)
        self.assertIn('未绑定平台用户', resp.json()['msg'])
        self.assertFalse(get_user_model().objects.filter(username='zhangsan').exists())

    def test_auto_create_when_enabled(self):
        FeishuApp.objects.filter(pk=FeishuApp.SINGLETON_PK).update(auto_create_user=True)
        resp = self._login()
        self.assertEqual(resp.status_code, 200)
        user = get_user_model().objects.get(email='zhangsan@corp.com')
        self.assertFalse(user.is_staff)
        # 不设可用密码：这类账号只能经飞书登录，也因此做不了要 sudo 的写操作
        self.assertFalse(user.has_usable_password())
        self.assertEqual(FeishuBinding.objects.get().source, 'auto_create')

    def test_inactive_user_rejected(self):
        user = get_user_model().objects.create(username='zs', is_active=False)
        FeishuBinding.objects.create(user=user, union_id='on_union_1')
        resp = self._login()
        self.assertEqual(resp.status_code, 401)
        self.assertIn('已停用', resp.json()['msg'])

    def test_service_account_rejected(self):
        user = get_user_model().objects.create(username='svc-ci', is_service=True)
        FeishuBinding.objects.create(user=user, union_id='on_union_1')
        resp = self._login()
        self.assertEqual(resp.status_code, 401)
        self.assertIn('服务账号', resp.json()['msg'])

    def test_state_cannot_be_reused(self):
        user = get_user_model().objects.create(username='zs')
        FeishuBinding.objects.create(user=user, union_id='on_union_1')
        state = self.client.get('/api/feishu/sso/authorize/').json()['data']['state']
        self.assertEqual(self._login(state=state).status_code, 200)
        second = self._login(state=state)
        self.assertEqual(second.status_code, 401)
        self.assertIn('登录状态已失效', second.json()['msg'])

    def test_missing_state_rejected(self):
        resp = self.client.post('/api/feishu/sso/callback/', {'code': 'c-1'}, format='json')
        self.assertEqual(resp.status_code, 401)
        self.assertIn('登录状态缺失', resp.json()['msg'])

    def test_missing_union_id_rejected(self):
        """拿不到 UnionID 通常是少了通讯录权限，报错要指向那里。"""
        get_user_model().objects.create(username='zs', email='zhangsan@corp.com')
        resp = self._login(profile={**PROFILE, 'union_id': ''})
        self.assertEqual(resp.status_code, 401)
        self.assertIn('UnionID', resp.json()['msg'])


class FeishuSsoMfaInteropTests(TestCase):
    """SSO 登录视为已过第二因子，但 sudo 不吃这一条。"""

    def setUp(self):
        cache.clear()
        from apps.system_cfg.models import SystemConfig

        SystemConfig.objects.update_or_create(
            key='mfa.enforce_scope', defaults={'value': 'all'})
        self.user = get_user_model().objects.create(username='sso_user', is_staff=True)
        self.user.set_password(PASSWORD)
        self.user.save()

    def _client_with(self, tokens):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION='Bearer ' + tokens['access'])
        return client

    def test_password_session_blocked_by_mfa_enforcement(self):
        from apps.auth_mgr.tokens import issue_access

        client = self._client_with(issue_access(self.user))
        resp = client.get('/api/assets/servers/')
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json().get('error_code'), 'mfa_enrollment_required')

    def test_sso_session_passes_mfa_enforcement(self):
        from apps.auth_mgr.tokens import issue_after_sso

        tokens = issue_after_sso(self.user)
        resp = self._client_with(tokens).get('/api/assets/servers/')
        self.assertEqual(resp.status_code, 200)

    def test_sso_session_still_needs_sudo_credentials(self):
        """飞书登录不替代 sudo：没给密码/验证码一律拒。"""
        from apps.auth_mgr.tokens import issue_after_sso

        client = self._client_with(issue_after_sso(self.user))
        resp = client.post('/api/auth/mfa/sudo/', {}, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_sso_session_can_sudo_with_password(self):
        from apps.auth_mgr.tokens import issue_after_sso

        client = self._client_with(issue_after_sso(self.user))
        resp = client.post('/api/auth/mfa/sudo/', {'password': PASSWORD}, format='json')
        self.assertEqual(resp.status_code, 200)


class FeishuMyBindingTests(TestCase):
    """个人页的绑定状态（只读，不提供自助解绑）。"""

    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create(username='bind_me')
        self.user.set_password(PASSWORD)
        self.user.save()
        self.client = APIClient()
        resp = self.client.post('/api/auth/login/',
                                {'username': 'bind_me', 'password': PASSWORD}, format='json')
        self.client.credentials(HTTP_AUTHORIZATION='Bearer ' + resp.json()['access'])

    def test_unbound(self):
        data = self.client.get('/api/feishu/sso/binding/').json()['data']
        self.assertFalse(data['bound'])
        self.assertFalse(data['sso_enabled'])

    def test_bound_shows_masked_union_id(self):
        FeishuBinding.objects.create(
            user=self.user, union_id='on_union_abcdef', open_id='ou_1',
            feishu_name='张三', feishu_email='z@corp.com', source='email')
        data = self.client.get('/api/feishu/sso/binding/').json()['data']
        self.assertTrue(data['bound'])
        self.assertEqual(data['feishu_name'], '张三')
        self.assertEqual(data['source_display'], '邮箱自动匹配')
        # union_id 只回尾段：展示只为确认「绑的是我这个飞书号」
        self.assertEqual(data['union_id_tail'], 'abcdef')
        self.assertNotIn('union_id', data)

    def test_requires_login(self):
        self.assertEqual(APIClient().get('/api/feishu/sso/binding/').status_code, 401)


# ══════════════════════════════════════════════════════════
# T6：飞书侧发起执行的准入判定
# ══════════════════════════════════════════════════════════

class FeishuAccessTests(TestCase):
    """四道闸：任务开关 → 默认全选 → 会话白名单 → 人的权限。"""

    def setUp(self):
        from apps.asset_mgr.models import Channel, Environment, ProjectGroup
        from apps.task_exec.models import ExecutionTask, TaskStep

        cache.clear()
        self.pg = ProjectGroup.objects.create(name='主项目', code='im-proj')
        self.ch = Channel.objects.create(name='官服', code='im-ch')
        self.env = Environment.objects.create(name='测试环境', code='im-env')
        self.prod = Environment.objects.create(name='生产环境', code='im-prod', is_prod=True)
        self.task = ExecutionTask.objects.create(
            name='重启网关', project_group=self.pg, channel=self.ch, environment=self.env,
            im_enabled=True, default_select_all=True,
        )
        TaskStep.objects.create(task=self.task, order=1, name='m1', command='echo hi')
        self.channel = FeishuChannel.objects.create(
            chat_id='oc_ops', name='运维群', allow_execute=True)
        # 无角色的 staff 回退为全权限 + 全能力位（roles.py 的存量兼容分支）
        self.admin = get_user_model().objects.create(username='im_admin', is_staff=True)
        self.member = get_user_model().objects.create(username='im_member', is_staff=False)

    def test_allows_admin_in_whitelisted_chat(self):
        self.assertIsNone(access.check(self.admin, self.task, 'oc_ops'))

    def test_unbound_user_rejected(self):
        """卡片上的 open_id 解析不到平台用户时，调用方传 None。"""
        self.assertIn('尚未绑定平台用户', access.check(None, self.task, 'oc_ops'))

    def test_task_switch_off_rejected(self):
        self.task.im_enabled = False
        self.task.save(update_fields=['im_enabled'])
        self.assertIn('未开启飞书执行', access.check(self.admin, self.task, 'oc_ops'))

    def test_manual_host_pick_task_rejected(self):
        """存量数据/直接改库造出的组合：关掉默认全选却开着飞书执行。"""
        self.task.default_select_all = False
        self.task.save(update_fields=['default_select_all'])
        self.assertIn('默认全选主机', access.check(self.admin, self.task, 'oc_ops'))

    def test_chat_not_whitelisted_rejected(self):
        self.assertIn('未被授权', access.check(self.admin, self.task, 'oc_other'))

    def test_disabled_chat_rejected(self):
        FeishuChannel.objects.filter(pk=self.channel.pk).update(enabled=False)
        self.assertIn('未被授权', access.check(self.admin, self.task, 'oc_ops'))

    def test_notify_only_chat_cannot_execute(self):
        """只收播报的群不给执行入口。"""
        FeishuChannel.objects.filter(pk=self.channel.pk).update(allow_execute=False)
        self.assertIn('未被授权', access.check(self.admin, self.task, 'oc_ops'))

    def test_capability_reused_from_trigger(self):
        """人的权限直接复用 task_exec.trigger.permission_error，不另写一份。"""
        reason = access.check(self.member, self.task, 'oc_ops')
        self.assertIn('没有发起任务执行的权限', reason)

    def test_production_capability_blocked(self):
        from apps.auth_mgr.models import Role

        role = Role.objects.create(
            code='im-ops', name='飞书运维',
            permissions={'exec': 'read'}, can_execute=True, can_exec_prod=False)
        self.member.role = role
        self.member.save(update_fields=['role'])
        self.task.environment = self.prod
        self.task.save(update_fields=['environment'])
        self.assertIn('不允许对生产环境', access.check(self.member, self.task, 'oc_ops'))

    def test_data_scope_blocked(self):
        from apps.asset_mgr.models import ProjectGroup

        other = ProjectGroup.objects.create(name='别的项目', code='im-other')
        self.admin.allowed_project_groups.add(other)
        self.assertIn('不在你的数据范围内', access.check(self.admin, self.task, 'oc_ops'))

    def test_needs_production_confirm(self):
        self.assertFalse(access.needs_production_confirm(self.task))
        self.task.environment = self.prod
        self.assertTrue(access.needs_production_confirm(self.task))

    # ── 任务下拉的可见范围 ────────────────────────────────
    def test_available_tasks_filters(self):
        from apps.task_exec.models import ExecutionTask

        ExecutionTask.objects.create(
            name='未开放', project_group=self.pg, channel=self.ch, environment=self.env,
            im_enabled=False)
        ExecutionTask.objects.create(
            name='要手选主机', project_group=self.pg, channel=self.ch, environment=self.env,
            im_enabled=True, default_select_all=False)
        names = list(access.available_tasks(self.admin, 'oc_ops').values_list('name', flat=True))
        self.assertEqual(names, ['重启网关'])

    def test_available_tasks_empty_for_unlisted_chat(self):
        self.assertEqual(access.available_tasks(self.admin, 'oc_other').count(), 0)

    def test_available_tasks_narrowed_by_data_scope(self):
        from apps.asset_mgr.models import ProjectGroup

        other = ProjectGroup.objects.create(name='别的项目', code='im-other')
        self.member.allowed_project_groups.add(other)
        self.assertEqual(access.available_tasks(self.member, 'oc_ops').count(), 0)


# ══════════════════════════════════════════════════════════
# T7：卡片模板（纯函数）
# ══════════════════════════════════════════════════════════

def _texts(payload):
    """递归收集卡片里的所有文本，便于断言「这句话出现了」。"""
    found = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key == 'content' and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_texts(value))
    elif isinstance(payload, list):
        for item in payload:
            found.extend(_texts(item))
    return found


def _bval(element):
    """取交互组件的回调值。

    卡片 2.0 把它挪到了 `behaviors[].value`，1.0 的顶层 `value` 不再被接受
    （飞书严格校验，两个都写整张卡都发不出去）。
    """
    for behavior in (element.get('behaviors') or []):
        if isinstance(behavior, dict) and 'value' in behavior:
            return behavior['value']
    return element.get('value')


def _buttons(payload):
    """收集卡片里的所有按钮。"""
    found = []
    if isinstance(payload, dict):
        if payload.get('tag') == 'button':
            found.append(payload)
        for value in payload.values():
            found.extend(_buttons(value))
    elif isinstance(payload, list):
        for item in payload:
            found.extend(_buttons(item))
    return found


class FeishuCardHelperTests(TestCase):
    def test_esc_neutralizes_markdown(self):
        """任务名里一个 * 或 ` 就能把整张卡片的结构带歪。"""
        raw = '重启*网关*_test_ `cmd` [x](y)'
        escaped = cards.esc(raw)
        for ch in ('*', '_', '`', '[', ']'):
            self.assertNotIn(ch, escaped)
        self.assertIn('重启', escaped)

    def test_esc_handles_none(self):
        self.assertEqual(cards.esc(None), '')

    def test_log_block_strips_backticks(self):
        """日志里自带 ``` 会让代码块围栏提前闭合，后面的内容当 markdown 渲染。"""
        block = cards.log_block('前面\n```\nrm -rf /\n```\n后面')
        content = block['content']
        self.assertEqual(content.count('```'), 2)   # 只有外层围栏
        self.assertIn("'''", content)               # 内层被替换成单引号

    def test_log_block_truncates(self):
        block = cards.log_block('\n'.join(f'line{i}' for i in range(100)))
        body = block['content']
        self.assertEqual(body.count('line'), cards.LOG_TAIL_LINES)
        self.assertIn('line99', body)               # 留的是尾段
        self.assertNotIn('line0\n', body)

    def test_log_block_truncates_long_line(self):
        block = cards.log_block('x' * 5000)
        self.assertLess(len(block['content']), cards.LOG_LINE_LIMIT + 50)

    def test_log_block_empty(self):
        self.assertIsNone(cards.log_block(''))
        self.assertIsNone(cards.log_block('   \n  '))

    def test_fmt_duration(self):
        self.assertEqual(cards.fmt_duration(None), '—')
        self.assertEqual(cards.fmt_duration(45), '45s')
        self.assertEqual(cards.fmt_duration(125), '2m05s')
        self.assertEqual(cards.fmt_duration(3725), '1h02m')


class FeishuCardTests(TestCase):
    def setUp(self):
        from apps.asset_mgr.models import Channel, Environment, ProjectGroup
        from apps.task_exec.models import ExecutionTask, TaskExecutionLog

        self.pg = ProjectGroup.objects.create(name='主项目', code='card-proj')
        self.ch = Channel.objects.create(name='官服', code='card-ch')
        self.env = Environment.objects.create(name='测试环境', code='card-env')
        self.prod = Environment.objects.create(name='生产环境', code='card-prod', is_prod=True)
        self.task = ExecutionTask.objects.create(
            name='重启网关', project_group=self.pg, channel=self.ch, environment=self.env,
            im_enabled=True)
        self.log = TaskExecutionLog.objects.create(
            task=self.task, status='running', trigger_type='feishu',
            servers_snapshot=[{'id': 1, 'name': 'web1', 'ip': '10.0.0.1'}],
            steps_result=[
                {'order': 1, 'name': '拉包', 'status': 'success', 'duration': 12},
                {'order': 2, 'name': '重启', 'status': 'running'},
                {'order': 3, 'name': '校验', 'status': 'pending'},
            ])

    def _to_prod(self):
        self.task.environment = self.prod
        self.task.save(update_fields=['environment'])
        return self.task

    # ── 任务选择 ──────────────────────────────────────────
    def test_picker_lists_tasks(self):
        payload = cards.task_picker([self.task])
        options = payload['body']['elements'][1]['options']
        self.assertEqual(options[0]['value'], str(self.task.id))
        self.assertIn('重启网关', options[0]['text']['content'])

    def test_picker_empty_has_no_select(self):
        """空下拉比一句说明更让人困惑。"""
        payload = cards.task_picker([])
        self.assertNotIn('select_static', json.dumps(payload))
        self.assertIn('没有可从飞书发起的任务', ''.join(_texts(payload)))

    # ── 确认卡 ────────────────────────────────────────────
    def test_detail_non_prod(self):
        payload = cards.task_detail(self.task, server_count=3, step_count=2)
        self.assertEqual(payload['header']['template'], 'blue')
        run = _buttons(payload)[0]
        self.assertEqual(_bval(run)['act'], cards.ACT_RUN)
        self.assertEqual(run['type'], 'primary')

    def test_detail_prod_is_red_and_danger(self):
        payload = cards.task_detail(self._to_prod(), server_count=3, step_count=2)
        self.assertEqual(payload['header']['template'], 'red')
        self.assertEqual(_buttons(payload)[0]['type'], 'danger')
        self.assertIn('生产', ''.join(_texts(payload)))

    def test_detail_without_hosts_offers_no_run(self):
        """让人点下去再看到「没有主机」的报错，不如现在就说清楚。"""
        payload = cards.task_detail(self.task, server_count=0, step_count=2)
        acts = [_bval(b)['act'] for b in _buttons(payload)]
        self.assertEqual(acts, [cards.ACT_CANCEL])
        self.assertIn('未匹配到可执行主机', ''.join(_texts(payload)))

    def test_production_confirm_states_the_target(self):
        """确认的意义是让人看清在对什么下手，只写「确认吗」等于没确认。"""
        payload = cards.production_confirm(
            self._to_prod(), server_count=12, step_count=4, token='tk1', ttl_seconds=60)
        text = ''.join(_texts(payload))
        self.assertIn('生产环境', text)
        self.assertIn('4 个模块 × 12 台主机', text)
        self.assertIn('60 秒内有效', text)
        confirm = _buttons(payload)[0]
        self.assertEqual(_bval(confirm),
                         {'act': cards.ACT_CONFIRM_PROD, 'task': self.task.id,
                          'token': 'tk1', 'p2p': False})

    # ── 状态卡 ────────────────────────────────────────────
    def test_progress_shows_step_icons(self):
        text = ''.join(_texts(cards.progress(self.task, self.log)))
        self.assertIn('✅ 拉包', text)
        self.assertIn('⏳ 重启', text)
        self.assertIn('⬜ 校验', text)

    def test_progress_truncates_long_flow(self):
        self.log.steps_result = [
            {'order': i, 'name': f'模块{i}', 'status': 'pending'} for i in range(1, 26)
        ]
        text = ''.join(_texts(cards.progress(self.task, self.log)))
        self.assertIn(f'还有 {25 - cards.MAX_STEPS_SHOWN} 个模块', text)

    def test_result_failed_has_tail_and_failed_names(self):
        self.log.status = 'failed'
        self.log.steps_result = [
            {'order': 1, 'name': '拉包', 'status': 'success', 'duration': 3},
            {'order': 2, 'name': '重启', 'status': 'failed', 'duration': 5},
        ]
        payload = cards.result(self.task, self.log, tail='boom\nfailed', url='https://x/y')
        text = ''.join(_texts(payload))
        self.assertEqual(payload['header']['template'], 'red')
        self.assertIn('失败模块', text)
        self.assertIn('重启', text)
        self.assertIn('boom', text)
        self.assertIn('https://x/y', text)

    def test_result_success_is_green_without_tail(self):
        self.log.status = 'success'
        payload = cards.result(self.task, self.log, tail='应当忽略')
        self.assertEqual(payload['header']['template'], 'green')
        self.assertNotIn('应当忽略', ''.join(_texts(payload)))

    # ── 暂停处理 ──────────────────────────────────────────
    def test_pause_confirm_buttons(self):
        payload = cards.pause(self.task, self.log, 'confirm', current_step='重启')
        dos = [_bval(b)['do'] for b in _buttons(payload)]
        self.assertEqual(dos, ['continue', 'abort'])

    def test_pause_failed_buttons_with_log(self):
        payload = cards.pause(self.task, self.log, 'failed',
                              current_step='重启', tail='Traceback...')
        dos = [_bval(b)['do'] for b in _buttons(payload)]
        self.assertEqual(dos, ['retry', 'skip', 'abort'])
        self.assertIn('Traceback...', ''.join(_texts(payload)))

    def test_pause_unknown_type_falls_back_to_confirm(self):
        payload = cards.pause(self.task, self.log, '', current_step=None)
        dos = [_bval(b)['do'] for b in _buttons(payload)]
        self.assertEqual(dos, ['continue', 'abort'])

    def test_pause_buttons_carry_log_id(self):
        """处理动作要落到具体这次执行上，不能只带任务 id。"""
        payload = cards.pause(self.task, self.log, 'failed')
        for btn in _buttons(payload):
            self.assertEqual(_bval(btn)['log'], self.log.id)

    # ── 定格与提示 ────────────────────────────────────────
    def test_handled_has_no_buttons(self):
        payload = cards.handled('已继续执行', by='张三')
        self.assertEqual(_buttons(payload), [])
        self.assertIn('张三', ''.join(_texts(payload)))

    def test_notice(self):
        payload = cards.notice('无法执行', '当前角色没有发起任务执行的权限。')
        self.assertIn('没有发起任务执行的权限', ''.join(_texts(payload)))

    # ── 体积边界 ──────────────────────────────────────────
    def test_large_flow_card_stays_small(self):
        """卡片有大小上限：50 个模块 + 长名字也不能撑爆。"""
        self.task.name = '超长任务名' * 40
        self.log.steps_result = [
            {'order': i, 'name': '模块名字很长' * 20, 'status': 'failed', 'duration': 9}
            for i in range(1, 51)
        ]
        self.log.status = 'failed'
        payload = cards.result(self.task, self.log, tail='x' * 10000)
        self.assertLess(len(json.dumps(payload, ensure_ascii=False)), 20000)


# ══════════════════════════════════════════════════════════
# T8：执行动作链路（事件 → 选择 → 确认 → 发起）
# ══════════════════════════════════════════════════════════

def _event_payload(text='执行', open_id='ou_1', chat_id='oc_ops',
                   event_type='im.message.receive_v1', union_id='on_1'):
    return {
        'schema': '2.0',
        'header': {'event_id': f'ev-{time.time_ns()}', 'event_type': event_type},
        'event': {
            'sender': {'sender_id': {'open_id': open_id, 'union_id': union_id}},
            'message': {'chat_id': chat_id, 'message_id': 'om_1',
                        'message_type': 'text',
                        'content': json.dumps({'text': f'@_user_1 {text}'})},
        },
    }


def _card_payload(value, *, open_id='ou_1', chat_id='oc_ops', option=None,
                  union_id='on_1'):
    action = {'tag': 'button', 'value': value}
    if option is not None:
        action = {'tag': 'select_static', 'value': value, 'option': option}
    return {
        'schema': '2.0',
        'header': {'event_id': f'ca-{time.time_ns()}', 'event_type': 'card.action.trigger'},
        'event': {
            'operator': {'open_id': open_id, 'union_id': union_id},
            'context': {'open_chat_id': chat_id, 'open_message_id': 'om_1'},
            'action': action,
        },
    }


@override_settings(TASK_EXEC_BACKEND='sync')
class FeishuHandlerTests(TestCase):
    """卡片动作链路。执行落到 task_exec.trigger，ansible 调用整体 mock 掉。"""

    def setUp(self):
        from apps.asset_mgr.models import Channel, Environment, ProjectGroup, ServerService, ServiceType
        from apps.asset_mgr.models import Server
        from apps.task_exec.models import ExecutionTask, TaskStep

        cache.clear()
        self.app = FeishuApp.load()
        self.pg = ProjectGroup.objects.create(name='主项目', code='h-proj')
        self.ch = Channel.objects.create(name='官服', code='h-ch')
        self.env = Environment.objects.create(name='测试环境', code='h-env')
        self.prod = Environment.objects.create(name='生产环境', code='h-prod', is_prod=True)
        self.service = ServiceType.objects.create(name='网关', code='h-gw')
        server = Server.objects.create(
            name='web1', private_ip='10.0.0.1',
            project_group=self.pg, channel=self.ch, environment=self.env)
        ServerService.objects.create(server=server, service_type=self.service, status='planned')
        prod_server = Server.objects.create(
            name='prod1', private_ip='10.0.1.1',
            project_group=self.pg, channel=self.ch, environment=self.prod)
        ServerService.objects.create(server=prod_server, service_type=self.service, status='planned')

        self.task = ExecutionTask.objects.create(
            name='重启网关', project_group=self.pg, channel=self.ch, environment=self.env,
            im_enabled=True, default_select_all=True)
        TaskStep.objects.create(task=self.task, order=1, name='重启',
                                command='echo hi', service_type=self.service)
        self.prod_task = ExecutionTask.objects.create(
            name='发版生产', project_group=self.pg, channel=self.ch, environment=self.prod,
            im_enabled=True, default_select_all=True)
        TaskStep.objects.create(task=self.prod_task, order=1, name='发版',
                                command='echo deploy', service_type=self.service)

        FeishuChannel.objects.create(chat_id='oc_ops', name='运维群', allow_execute=True)
        self.user = get_user_model().objects.create(username='ops_zhang', is_staff=True)
        FeishuBinding.objects.create(user=self.user, union_id='on_1', open_id='ou_1')

    def _run_card(self, payload):
        """执行一次卡片动作，ansible 那步不真跑。"""
        with patch('apps.task_exec.tasks.execute_task') as runner:
            resp = handlers.handle_card_action(self.app, payload)
        return resp, runner

    # ── 事件：@机器人 ─────────────────────────────────────
    def test_event_sends_task_picker(self):
        with patch('apps.im_mgr.tasks.dispatch_send_card') as send:
            handlers.handle_event(self.app, _event_payload())
        card = send.call_args[0][1]
        self.assertIn('重启网关', ''.join(_texts(card)))

    def test_event_from_unbound_account(self):
        with patch('apps.im_mgr.tasks.dispatch_send_card') as send:
            handlers.handle_event(self.app, _event_payload(
                open_id='ou_stranger', union_id='on_stranger'))
        text = ''.join(_texts(send.call_args[0][1]))
        self.assertIn('还没有绑定平台用户', text)

    def test_event_help(self):
        with patch('apps.im_mgr.tasks.dispatch_send_card') as send:
            handlers.handle_event(self.app, _event_payload(text='帮助'))
        self.assertIn('运维机器人', ''.join(_texts(send.call_args[0][1])))

    def test_event_other_type_ignored(self):
        with patch('apps.im_mgr.tasks.dispatch_send_card') as send:
            handlers.handle_event(self.app, _event_payload(event_type='im.chat.updated_v1'))
        send.assert_not_called()

    def test_event_in_unlisted_chat_says_why(self):
        """未授权的群要说清原因：空下拉只会让人以为「没有任务」。"""
        with patch('apps.im_mgr.tasks.dispatch_send_card') as send:
            handlers.handle_event(self.app, _event_payload(chat_id='oc_random'))
        text = ''.join(_texts(send.call_args[0][1]))
        self.assertIn('未被授权', text)

    # ── 选择任务 ──────────────────────────────────────────
    def test_pick_task_returns_detail(self):
        payload = _card_payload({'act': cards.ACT_PICK_TASK}, option=str(self.task.id))
        resp, _ = self._run_card(payload)
        text = ''.join(_texts(resp['card']['data']))
        self.assertIn('重启网关', text)
        self.assertIn('当前匹配 1 台', text)

    def test_pick_task_missing(self):
        payload = _card_payload({'act': cards.ACT_PICK_TASK}, option='99999')
        resp, _ = self._run_card(payload)
        self.assertIn('已被删除', ''.join(_texts(resp['card']['data'])))

    def test_pick_task_rechecks_access(self):
        """卡片可能是十分钟前发的，开关期间可能被关掉。"""
        self.task.im_enabled = False
        self.task.save(update_fields=['im_enabled'])
        payload = _card_payload({'act': cards.ACT_PICK_TASK}, option=str(self.task.id))
        resp, _ = self._run_card(payload)
        self.assertIn('未开启飞书执行', ''.join(_texts(resp['card']['data'])))

    # ── 非生产执行 ────────────────────────────────────────
    def test_run_launches_and_audits(self):
        from apps.audit.models import AuditLog
        from apps.task_exec.models import TaskExecutionLog

        payload = _card_payload({'act': cards.ACT_RUN, 'task': self.task.id})
        resp, runner = self._run_card(payload)
        runner.assert_called_once()
        log = TaskExecutionLog.objects.get(task=self.task)
        self.assertEqual(log.trigger_type, 'feishu')
        self.assertEqual(log.triggered_by_id, self.user.id)
        self.assertEqual(resp['toast']['type'], 'success')
        # 回调路径整段不入审计，但「谁从飞书发起了执行」必须留痕
        entry = AuditLog.objects.get(user=self.user)
        self.assertNotIn(self.app.callback_slug, entry.path)   # 不把回调随机串写进审计
        self.assertIn('"task_id"', entry.body)

    def test_run_unbound_user_rejected(self):
        payload = _card_payload({'act': cards.ACT_RUN, 'task': self.task.id},
                                open_id='ou_stranger', union_id='on_stranger')
        resp, runner = self._run_card(payload)
        runner.assert_not_called()
        self.assertIn('还没有绑定平台用户', ''.join(_texts(resp['card']['data'])))

    def test_run_in_unlisted_chat_rejected(self):
        payload = _card_payload({'act': cards.ACT_RUN, 'task': self.task.id},
                                chat_id='oc_random')
        resp, runner = self._run_card(payload)
        runner.assert_not_called()
        self.assertIn('未被授权', ''.join(_texts(resp['card']['data'])))

    def test_cancel(self):
        resp, runner = self._run_card(_card_payload({'act': cards.ACT_CANCEL}))
        runner.assert_not_called()
        self.assertIn('已取消', ''.join(_texts(resp['card']['data'])))

    # ── 生产二次确认 ──────────────────────────────────────
    def test_prod_run_requires_confirm(self):
        payload = _card_payload({'act': cards.ACT_RUN, 'task': self.prod_task.id})
        resp, runner = self._run_card(payload)
        runner.assert_not_called()
        text = ''.join(_texts(resp['card']['data']))
        self.assertIn('生产环境', text)
        self.assertEqual(FeishuConfirmToken.objects.count(), 1)

    def _issue_prod_token(self):
        payload = _card_payload({'act': cards.ACT_RUN, 'task': self.prod_task.id})
        self._run_card(payload)
        return FeishuConfirmToken.objects.get().token

    def test_prod_confirm_launches(self):
        from apps.task_exec.models import TaskExecutionLog

        token = self._issue_prod_token()
        resp, runner = self._run_card(
            _card_payload({'act': cards.ACT_CONFIRM_PROD, 'token': token}))
        runner.assert_called_once()
        self.assertEqual(TaskExecutionLog.objects.filter(task=self.prod_task).count(), 1)
        self.assertEqual(resp['toast']['type'], 'success')

    def test_prod_confirm_by_another_person_rejected(self):
        """群里的卡片谁都点得到：不限人就等于「A 发起、B 确认」绕过 A 的判定。"""
        other = get_user_model().objects.create(username='ops_li', is_staff=True)
        FeishuBinding.objects.create(user=other, union_id='on_2', open_id='ou_2')
        token = self._issue_prod_token()
        payload = _card_payload({'act': cards.ACT_CONFIRM_PROD, 'token': token},
                                open_id='ou_2', union_id='on_2')
        resp, runner = self._run_card(payload)
        runner.assert_not_called()
        self.assertIn('只有发起人本人', ''.join(_texts(resp['card']['data'])))

    def test_prod_confirm_is_single_use(self):
        token = self._issue_prod_token()
        first, runner1 = self._run_card(
            _card_payload({'act': cards.ACT_CONFIRM_PROD, 'token': token}))
        runner1.assert_called_once()
        second, runner2 = self._run_card(
            _card_payload({'act': cards.ACT_CONFIRM_PROD, 'token': token}))
        runner2.assert_not_called()
        self.assertIn('已处理过', ''.join(_texts(second['card']['data'])))

    def test_prod_confirm_expires(self):
        from django.utils import timezone

        token = self._issue_prod_token()
        FeishuConfirmToken.objects.filter(token=token).update(
            expires_at=timezone.now() - timedelta(seconds=1))
        resp, runner = self._run_card(
            _card_payload({'act': cards.ACT_CONFIRM_PROD, 'token': token}))
        runner.assert_not_called()
        self.assertIn('超时', ''.join(_texts(resp['card']['data'])))

    def test_prod_confirm_unknown_token(self):
        resp, runner = self._run_card(
            _card_payload({'act': cards.ACT_CONFIRM_PROD, 'token': 'not-a-token'}))
        runner.assert_not_called()
        self.assertIn('票据无效', ''.join(_texts(resp['card']['data'])))

    def test_prod_confirm_rechecks_capability(self):
        """票据只证明「确认有效」，权限仍按当下重判。"""
        from apps.auth_mgr.models import Role

        token = self._issue_prod_token()
        role = Role.objects.create(code='h-noprod', name='不可生产',
                                   permissions={'exec': 'read'},
                                   can_execute=True, can_exec_prod=False)
        self.user.role = role
        self.user.save(update_fields=['role'])
        resp, runner = self._run_card(
            _card_payload({'act': cards.ACT_CONFIRM_PROD, 'token': token}))
        runner.assert_not_called()
        self.assertIn('不允许对生产环境', ''.join(_texts(resp['card']['data'])))

    # ── payload 形态兼容 ──────────────────────────────────
    def test_value_as_json_string(self):
        """部分客户端把 value 序列化成字符串回传。"""
        payload = _card_payload({'act': cards.ACT_CANCEL})
        payload['event']['action']['value'] = json.dumps({'act': cards.ACT_CANCEL})
        resp, _ = self._run_card(payload)
        self.assertIn('已取消', ''.join(_texts(resp['card']['data'])))

    def test_v1_payload_shape(self):
        """旧版卡片回调把 open_id 与 action 放在顶层。"""
        payload = {
            'open_id': 'ou_1',
            'open_chat_id': 'oc_ops',
            'open_message_id': 'om_1',
            'action': {'tag': 'button', 'value': {'act': cards.ACT_RUN, 'task': self.task.id}},
        }
        resp, runner = self._run_card(payload)
        runner.assert_called_once()
        self.assertEqual(resp['toast']['type'], 'success')

    def test_unknown_action_ignored(self):
        resp, runner = self._run_card(_card_payload({'act': 'nope'}))
        runner.assert_not_called()
        self.assertIsNone(resp)


# ══════════════════════════════════════════════════════════
# T9：执行进度回显（就地刷新同一张卡片）
# ══════════════════════════════════════════════════════════

@override_settings(TASK_EXEC_BACKEND='sync', SITE_BASE_URL='https://ops.example.com')
class FeishuProgressTests(TestCase):
    def setUp(self):
        from apps.asset_mgr.models import Channel, Environment, ProjectGroup
        from apps.task_exec.models import ExecutionTask, TaskExecutionLog

        cache.clear()
        self.app = FeishuApp.load()
        for key, value in FULL_CONFIG.items():
            setattr(self.app, key, value)
        self.app.enabled = True
        self.app.save()
        pg = ProjectGroup.objects.create(name='主项目', code='p-proj')
        ch = Channel.objects.create(name='官服', code='p-ch')
        env = Environment.objects.create(name='测试环境', code='p-env')
        self.task = ExecutionTask.objects.create(
            name='重启网关', project_group=pg, channel=ch, environment=env, im_enabled=True)
        self.log = TaskExecutionLog.objects.create(
            task=self.task, status='running', trigger_type='feishu',
            steps_result=[{'order': 1, 'name': '重启', 'status': 'running'}])

    def _bind(self):
        return notify.bind(self.log, 'oc_ops', 'om_1')

    # ── 绑定 ──────────────────────────────────────────────
    def test_bind_and_rebind(self):
        self._bind()
        notify.bind(self.log, 'oc_ops', 'om_2')
        self.assertEqual(FeishuCardMessage.objects.count(), 1)
        self.assertEqual(FeishuCardMessage.objects.get().message_id, 'om_2')

    def test_bind_skips_without_message_id(self):
        self.assertIsNone(notify.bind(self.log, 'oc_ops', ''))
        self.assertFalse(FeishuCardMessage.objects.exists())

    # ── 调度与节流 ────────────────────────────────────────
    def test_no_dispatch_without_binding(self):
        """页面发起的执行没绑卡片，整条通知链路要在最早处短路。"""
        with patch('apps.im_mgr.tasks.dispatch_refresh_card') as job:
            notify.on_execution_updated(None, task_id=self.task.id, log_id=self.log.id)
        job.assert_not_called()

    def test_dispatch_when_bound(self):
        self._bind()
        with patch('apps.im_mgr.tasks.dispatch_refresh_card') as job:
            notify.on_execution_updated(None, task_id=self.task.id, log_id=self.log.id)
        job.assert_called_once_with(self.log.id)

    def test_intermediate_updates_are_throttled(self):
        self._bind()
        with patch('apps.im_mgr.tasks.dispatch_refresh_card') as job:
            for _ in range(5):
                notify.on_execution_updated(None, task_id=self.task.id, log_id=self.log.id)
        self.assertEqual(job.call_count, 1)

    def test_terminal_status_bypasses_throttle(self):
        """中间态丢一帧无所谓，终态丢了就永远停在「执行中」。"""
        self._bind()
        with patch('apps.im_mgr.tasks.dispatch_refresh_card') as job:
            notify.on_execution_updated(None, task_id=self.task.id, log_id=self.log.id)
            self.log.status = 'success'
            self.log.save(update_fields=['status'])
            notify.on_execution_updated(None, task_id=self.task.id, log_id=self.log.id)
        self.assertEqual(job.call_count, 2)

    def test_resolves_latest_log_when_only_task_given(self):
        """push_status 只给 task_id。"""
        self._bind()
        with patch('apps.im_mgr.tasks.dispatch_refresh_card') as job:
            notify.on_execution_updated(None, task_id=self.task.id, log_id=None)
        job.assert_called_once_with(self.log.id)

    def test_receiver_never_raises(self):
        """信号在执行线程里同步触发，接收端抛异常会把任务执行带停。"""
        with patch('apps.im_mgr.notify._schedule', side_effect=RuntimeError('boom')):
            notify.on_execution_updated(None, task_id=self.task.id, log_id=self.log.id)

    # ── 渲染与发送 ────────────────────────────────────────
    def test_refresh_sends_progress_card(self):
        self._bind()
        with patch('apps.im_mgr.client.update_card') as update:
            notify.refresh(self.log.id)
        message_id, card = update.call_args[0]
        self.assertEqual(message_id, 'om_1')
        text = ''.join(_texts(card))
        self.assertIn('⏳ 重启', text)
        self.assertIn(f'https://ops.example.com/task/executions/{self.task.id}/logs/{self.log.id}',
                      text)

    def test_refresh_sends_result_card_on_finish(self):
        self._bind()
        self.log.status = 'failed'
        self.log.output = '致命错误：端口被占用'
        self.log.steps_result = [{'order': 1, 'name': '重启', 'status': 'failed'}]
        self.log.save()
        with patch('apps.im_mgr.client.update_card') as update:
            notify.refresh(self.log.id)
        card = update.call_args[0][1]
        self.assertEqual(card['header']['template'], 'red')
        self.assertIn('端口被占用', ''.join(_texts(card)))

    def test_refresh_reads_current_state_not_scheduled_state(self):
        """异步之间没有顺序保证：晚到的进度帧必须渲染出当下（已终态）的样子。"""
        self._bind()
        from apps.task_exec.models import TaskExecutionLog

        TaskExecutionLog.objects.filter(pk=self.log.id).update(status='success')
        with patch('apps.im_mgr.client.update_card') as update:
            notify.refresh(self.log.id)   # 按「执行中」时投递的帧来刷
        card = update.call_args[0][1]
        self.assertEqual(card['header']['template'], 'green')

    def test_refresh_noop_without_binding(self):
        with patch('apps.im_mgr.client.update_card') as update:
            notify.refresh(self.log.id)
        update.assert_not_called()

    def test_refresh_noop_when_feishu_disabled(self):
        self._bind()
        FeishuApp.objects.filter(pk=FeishuApp.SINGLETON_PK).update(enabled=False)
        with patch('apps.im_mgr.client.update_card') as update:
            notify.refresh(self.log.id)
        update.assert_not_called()

    def test_feishu_failure_does_not_escape(self):
        """飞书发不出去是飞书的事，绝不能回写任务状态或抛回执行链路。"""
        self._bind()
        with patch('apps.im_mgr.client.update_card',
                   side_effect=client.FeishuError('bot not in chat')):
            tasks.refresh_card(self.log.id)
        self.log.refresh_from_db()
        self.assertEqual(self.log.status, 'running')

    # ── 与执行链路对接 ────────────────────────────────────
    def test_live_steps_update_triggers_refresh(self):
        """真正的触发点：live.publish_steps 写完模块表就广播。"""
        from apps.task_exec.live import LiveSink

        self._bind()
        sink = LiveSink(self.task.id, self.log.id)
        with patch('apps.im_mgr.tasks.dispatch_refresh_card') as job:
            sink.bind_steps([{'order': 1, 'name': '重启', 'status': 'running'}])
        job.assert_called_once_with(self.log.id)

    def test_launch_binds_card_message(self):
        """从飞书发起时绑的是被按钮替换掉的那条消息。"""
        from apps.asset_mgr.models import Server, ServerService, ServiceType
        from apps.task_exec.models import TaskStep

        service = ServiceType.objects.create(name='网关', code='p-gw')
        server = Server.objects.create(
            name='web1', private_ip='10.0.0.1',
            project_group=self.task.project_group, channel=self.task.channel,
            environment=self.task.environment)
        ServerService.objects.create(server=server, service_type=service, status='planned')
        TaskStep.objects.create(task=self.task, order=1, name='m',
                                command='echo hi', service_type=service)
        user = get_user_model().objects.create(username='p_ops', is_staff=True)
        FeishuBinding.objects.create(user=user, union_id='on_p', open_id='ou_p')
        FeishuChannel.objects.create(chat_id='oc_ops', name='运维群', allow_execute=True)
        self.task.default_select_all = True
        self.task.save(update_fields=['default_select_all'])

        payload = _card_payload({'act': cards.ACT_RUN, 'task': self.task.id},
                                open_id='ou_p', union_id='on_p')
        with patch('apps.task_exec.tasks.execute_task'):
            handlers.handle_card_action(self.app, payload)
        binding = FeishuCardMessage.objects.exclude(log=self.log).get()
        self.assertEqual(binding.message_id, 'om_1')
        self.assertEqual(binding.chat_id, 'oc_ops')


# ══════════════════════════════════════════════════════════
# T10：暂停确认（等待确认 / 失败待处理）
# ══════════════════════════════════════════════════════════

@override_settings(TASK_EXEC_BACKEND='sync', SITE_BASE_URL='https://ops.example.com')
class FeishuPauseTests(TestCase):
    def setUp(self):
        from apps.asset_mgr.models import Channel, Environment, ProjectGroup
        from apps.task_exec.models import ExecutionTask, TaskExecutionLog, TaskStep

        cache.clear()
        self.app = FeishuApp.load()
        for key, value in FULL_CONFIG.items():
            setattr(self.app, key, value)
        self.app.enabled = True
        self.app.save()
        pg = ProjectGroup.objects.create(name='主项目', code='pz-proj')
        ch = Channel.objects.create(name='官服', code='pz-ch')
        env = Environment.objects.create(name='测试环境', code='pz-env')
        self.user = get_user_model().objects.create(username='pz_ops', is_staff=True)
        FeishuBinding.objects.create(user=self.user, union_id='on_1', open_id='ou_1')
        self.task = ExecutionTask.objects.create(
            name='发布流程', project_group=pg, channel=ch, environment=env,
            im_enabled=True, status='paused', pause_type='confirm')
        TaskStep.objects.create(task=self.task, order=1, name='人工确认', task_type='pause')
        self.log = TaskExecutionLog.objects.create(
            task=self.task, status='paused', trigger_type='feishu',
            triggered_by=self.user,
            steps_result=[
                {'order': 1, 'name': '拉包', 'status': 'success', 'duration': 3},
                {'order': 2, 'name': '人工确认', 'status': 'running'},
            ])

    def _pause_payload(self, do, *, log_id=None, chat_id='oc_ops', open_id='ou_1',
                       union_id='on_1'):
        return _card_payload(
            {'act': cards.ACT_PAUSE, 'task': self.task.id,
             'log': log_id or self.log.id, 'do': do},
            chat_id=chat_id, open_id=open_id, union_id=union_id)

    # ── 渲染 ──────────────────────────────────────────────
    def test_render_confirm_pause(self):
        card = notify.render(self.log)
        dos = [_bval(b)['do'] for b in _buttons(card)]
        self.assertEqual(dos, ['continue', 'abort'])
        self.assertIn('人工确认', ''.join(_texts(card)))

    def test_render_failed_pause_includes_log(self):
        """要判断该重试还是中止，靠的就是这几行日志。"""
        self.task.pause_type = 'failed'
        self.task.save(update_fields=['pause_type'])
        self.log.output = 'fatal: 连接超时'
        self.log.steps_result = [{'order': 1, 'name': '重启', 'status': 'failed'}]
        self.log.save()
        card = notify.render(self.log)
        dos = [_bval(b)['do'] for b in _buttons(card)]
        self.assertEqual(dos, ['retry', 'skip', 'abort'])
        text = ''.join(_texts(card))
        self.assertIn('连接超时', text)
        self.assertIn('重启', text)

    def test_render_confirm_pause_has_no_log_block(self):
        """等待确认不是失败，贴一段日志只会让人以为出错了。"""
        self.log.output = '不该出现在卡片上'
        self.log.save(update_fields=['output'])
        self.assertNotIn('不该出现在卡片上', ''.join(_texts(notify.render(self.log))))

    # ── 处理动作 ──────────────────────────────────────────
    def test_continue_writes_pause_action(self):
        from apps.audit.models import AuditLog
        from apps.task_exec.models import ExecutionTask

        resp = handlers.handle_card_action(self.app, self._pause_payload('continue'))
        self.task.refresh_from_db()
        self.assertEqual(ExecutionTask.objects.get(pk=self.task.id).pause_action, 'continue')
        self.assertIn('已继续执行', ''.join(_texts(resp['card']['data'])))
        self.assertEqual(_buttons(resp['card']['data']), [])   # 定格，防重复点
        self.assertTrue(AuditLog.objects.filter(user=self.user).exists())

    def test_action_must_match_pause_type(self):
        """failed 状态收到 continue 信号会让 runner 走错分支。"""
        resp = handlers.handle_card_action(self.app, self._pause_payload('retry'))
        self.assertIn('仅支持动作', ''.join(_texts(resp['card']['data'])))

    def test_failed_pause_accepts_retry(self):
        from apps.task_exec.models import ExecutionTask

        ExecutionTask.objects.filter(pk=self.task.id).update(pause_type='failed')
        handlers.handle_card_action(self.app, self._pause_payload('retry'))
        self.assertEqual(ExecutionTask.objects.get(pk=self.task.id).pause_action, 'retry')

    def test_rejected_when_not_paused(self):
        from apps.task_exec.models import ExecutionTask

        ExecutionTask.objects.filter(pk=self.task.id).update(status='running')
        resp = handlers.handle_card_action(self.app, self._pause_payload('continue'))
        self.assertIn('不在「等待处理」状态', ''.join(_texts(resp['card']['data'])))

    def test_double_click_only_takes_once(self):
        from apps.task_exec.models import ExecutionTask

        first = handlers.handle_card_action(self.app, self._pause_payload('continue'))
        # runner 消费掉信号并恢复执行
        ExecutionTask.objects.filter(pk=self.task.id).update(status='running', pause_action='')
        second = handlers.handle_card_action(self.app, self._pause_payload('continue'))
        self.assertIn('已继续执行', ''.join(_texts(first['card']['data'])))
        self.assertIn('不在「等待处理」状态', ''.join(_texts(second['card']['data'])))

    def test_stale_card_from_previous_run_rejected(self):
        """任务本体的 paused 只属于最近一次执行，拿旧卡片点等于对着另一次执行下指令。"""
        from apps.task_exec.models import TaskExecutionLog

        newer = TaskExecutionLog.objects.create(task=self.task, status='paused')
        resp = handlers.handle_card_action(self.app, self._pause_payload('continue'))
        self.assertIn('对应的执行已经结束', ''.join(_texts(resp['card']['data'])))
        self.assertIsNotNone(newer)

    def test_unknown_log_rejected(self):
        resp = handlers.handle_card_action(self.app, self._pause_payload('continue', log_id=99999))
        self.assertIn('已被删除', ''.join(_texts(resp['card']['data'])))

    # ── 权限 ──────────────────────────────────────────────
    def test_chat_whitelist_not_required(self):
        """私聊推过来的卡片，那个会话不可能在白名单里。"""
        resp = handlers.handle_card_action(
            self.app, self._pause_payload('continue', chat_id='p2p_random'))
        self.assertIn('已继续执行', ''.join(_texts(resp['card']['data'])))

    def test_capability_required(self):
        """continue/retry/skip 都会让剩余模块真跑起来，只在发起那头拦是留后门。"""
        other = get_user_model().objects.create(username='pz_member', is_staff=False)
        FeishuBinding.objects.create(user=other, union_id='on_2', open_id='ou_2')
        resp = handlers.handle_card_action(
            self.app, self._pause_payload('continue', open_id='ou_2', union_id='on_2'))
        self.assertIn('没有发起任务执行的权限', ''.join(_texts(resp['card']['data'])))

    def test_unbound_user_rejected(self):
        resp = handlers.handle_card_action(
            self.app, self._pause_payload('continue', open_id='ou_x', union_id='on_x'))
        self.assertIn('还没有绑定平台用户', ''.join(_texts(resp['card']['data'])))

    # ── 页面发起的任务：私聊推送 ──────────────────────────
    def test_paused_without_card_triggers_private_push(self):
        with patch('apps.im_mgr.tasks.dispatch_pause_push') as job:
            notify.on_execution_updated(None, task_id=self.task.id, log_id=self.log.id)
        job.assert_called_once_with(self.log.id)

    def test_push_pause_sends_and_binds(self):
        with patch('apps.im_mgr.client.send_card', return_value='om_p') as send:
            notify.push_pause(self.log.id)
        receive_id, card = send.call_args[0][:2]
        self.assertEqual(receive_id, 'ou_1')
        self.assertEqual(send.call_args[1]['receive_id_type'], 'open_id')
        self.assertEqual(FeishuCardMessage.objects.get(log=self.log).message_id, 'om_p')
        self.assertIn('人工确认', ''.join(_texts(card)))

    def test_push_pause_only_once(self):
        """失败重试后再次失败会重复广播 paused，不能每次都推一条新消息。"""
        with patch('apps.im_mgr.client.send_card', return_value='om_p') as send:
            notify.push_pause(self.log.id)
            notify.push_pause(self.log.id)
        self.assertEqual(send.call_count, 1)

    def test_push_pause_skips_when_already_bound(self):
        notify.bind(self.log, 'oc_ops', 'om_1')
        with patch('apps.im_mgr.client.send_card') as send:
            notify.push_pause(self.log.id)
        send.assert_not_called()

    def test_push_pause_skips_unbound_operator(self):
        from apps.task_exec.models import TaskExecutionLog

        stranger = get_user_model().objects.create(username='pz_nofeishu')
        TaskExecutionLog.objects.filter(pk=self.log.id).update(triggered_by=stranger)
        with patch('apps.im_mgr.client.send_card') as send:
            notify.push_pause(self.log.id)
        send.assert_not_called()

    def test_push_pause_skips_when_no_longer_paused(self):
        from apps.task_exec.models import TaskExecutionLog

        TaskExecutionLog.objects.filter(pk=self.log.id).update(status='running')
        with patch('apps.im_mgr.client.send_card') as send:
            notify.push_pause(self.log.id)
        send.assert_not_called()

    def test_bound_card_refreshes_into_pause_card(self):
        """从飞书发起的执行：暂停时就地刷成处理卡，不另发消息。"""
        notify.bind(self.log, 'oc_ops', 'om_1')
        with patch('apps.im_mgr.client.update_card') as update, \
                patch('apps.im_mgr.client.send_card') as send:
            notify.on_execution_updated(None, task_id=self.task.id, log_id=self.log.id)
        send.assert_not_called()
        dos = [_bval(b)['do'] for b in _buttons(update.call_args[0][1])]
        self.assertEqual(dos, ['continue', 'abort'])


# ══════════════════════════════════════════════════════════
# T11：绑定关系管理（account 模块）
# ══════════════════════════════════════════════════════════

class FeishuBindingAdminTests(TestCase):
    def setUp(self):
        cache.clear()
        self.app = FeishuApp.load()
        for key, value in FULL_CONFIG.items():
            setattr(self.app, key, value)
        self.app.enabled = True
        self.app.save()
        self.target = get_user_model().objects.create(
            username='zhangsan', email='zhangsan@corp.com')

    def _client(self, is_staff=True, username=None, sudo=True):
        username = username or ('admin_bind' if is_staff else 'user_bind')
        user = get_user_model().objects.create(username=username, is_staff=is_staff)
        user.set_password(PASSWORD)
        user.save()
        client_ = APIClient()
        resp = client_.post('/api/auth/login/',
                            {'username': username, 'password': PASSWORD}, format='json')
        client_.credentials(HTTP_AUTHORIZATION='Bearer ' + resp.json()['access'])
        if is_staff and sudo:
            grant_sudo(client_)
        return client_

    def test_create_and_list(self):
        admin = self._client()
        resp = admin.post('/api/feishu/bindings/', {
            'user': self.target.id, 'union_id': 'on_new', 'open_id': 'ou_new',
            'feishu_name': '张三', 'feishu_email': 'zhangsan@corp.com',
        }, format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(FeishuBinding.objects.get().source, 'manual')
        listed = admin.get('/api/feishu/bindings/?keyword=zhang').json()['data']
        self.assertEqual(listed['total'], 1)
        self.assertEqual(listed['results'][0]['username'], 'zhangsan')

    def test_duplicate_union_id_names_the_holder(self):
        """默认报错看不出是谁占着，而管理员最需要知道的恰恰是这个。"""
        FeishuBinding.objects.create(user=self.target, union_id='on_taken')
        other = get_user_model().objects.create(username='lisi')
        admin = self._client()
        resp = admin.post('/api/feishu/bindings/', {
            'user': other.id, 'union_id': 'on_taken',
        }, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('zhangsan', resp.json()['msg'])

    def test_user_already_bound(self):
        FeishuBinding.objects.create(user=self.target, union_id='on_a')
        admin = self._client()
        resp = admin.post('/api/feishu/bindings/', {
            'user': self.target.id, 'union_id': 'on_b',
        }, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('已绑定飞书账号', resp.json()['msg'])

    def test_service_account_rejected(self):
        svc = get_user_model().objects.create(username='svc-ci', is_service=True)
        admin = self._client()
        resp = admin.post('/api/feishu/bindings/', {
            'user': svc.id, 'union_id': 'on_svc',
        }, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('服务账号', resp.json()['msg'])

    def test_destroy(self):
        binding = FeishuBinding.objects.create(user=self.target, union_id='on_x')
        admin = self._client()
        resp = admin.delete(f'/api/feishu/bindings/{binding.id}/')
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(FeishuBinding.objects.exists())

    def test_no_update_endpoint(self):
        """改绑 = 解绑再绑，两步各自留痕比一次静默改值清楚。"""
        binding = FeishuBinding.objects.create(user=self.target, union_id='on_x')
        admin = self._client()
        resp = admin.put(f'/api/feishu/bindings/{binding.id}/',
                         {'user': self.target.id, 'union_id': 'on_y'}, format='json')
        self.assertEqual(resp.status_code, 405)

    def test_requires_account_permission(self):
        """绑定列表本身就是一份「谁能从飞书进来」的清单，读也要 write 档。"""
        member = self._client(is_staff=False)
        self.assertEqual(member.get('/api/feishu/bindings/').status_code, 403)

    def test_write_requires_sudo(self):
        admin = self._client(username='admin_nosudo_bind', sudo=False)
        resp = admin.post('/api/feishu/bindings/', {
            'user': self.target.id, 'union_id': 'on_s',
        }, format='json')
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json().get('error_code'), 'sudo_required')

    # ── 通讯录反查 ────────────────────────────────────────
    def test_lookup_returns_ids_and_name(self):
        admin = self._client()
        with patch('apps.im_mgr.client.lookup_user_ids',
                   return_value={'union_id': 'on_1', 'open_id': 'ou_1'}), \
                patch('apps.im_mgr.client.get_user', return_value={'name': '张三'}):
            resp = admin.post('/api/feishu/bindings/lookup/',
                              {'email': 'zhangsan@corp.com'}, format='json')
        data = resp.json()['data']
        self.assertEqual(data['union_id'], 'on_1')
        self.assertEqual(data['name'], '张三')

    def test_lookup_not_found(self):
        """查不到就明说，不返回半个 id——那样建出的绑定永远匹配不上回调身份。"""
        admin = self._client()
        with patch('apps.im_mgr.client.lookup_user_ids', return_value={}):
            resp = admin.post('/api/feishu/bindings/lookup/',
                              {'email': 'nobody@corp.com'}, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('未找到', resp.json()['msg'])

    def test_lookup_requires_input(self):
        admin = self._client()
        resp = admin.post('/api/feishu/bindings/lookup/', {}, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_lookup_survives_name_query_failure(self):
        """名字只是给人看的，查不到不该让整次反查失败。"""
        admin = self._client()
        with patch('apps.im_mgr.client.lookup_user_ids',
                   return_value={'union_id': 'on_1', 'open_id': 'ou_1'}), \
                patch('apps.im_mgr.client.get_user',
                      side_effect=client.FeishuError('no permission')):
            resp = admin.post('/api/feishu/bindings/lookup/',
                              {'email': 'zhangsan@corp.com'}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['data']['name'], '')


class FeishuUrlVerificationTests(TestCase):
    """事件订阅地址验证：飞书**不给这一次请求带签名头**。

    联调时踩到的：实现原本把 verify_signature 放在最前面，对验证请求也强制要求
    签名，于是飞书后台永远显示「Challenge code 没有返回」。
    """

    ENCRYPT_KEY = 'encrypt-value'
    TOKEN = 'token-value'

    def setUp(self):
        cache.clear()
        self.app = FeishuApp.load()
        for key, value in FULL_CONFIG.items():
            setattr(self.app, key, value)
        self.app.ip_whitelist = '127.0.0.1'
        self.app.enabled = True
        self.app.save()
        self.client = APIClient()

    def _post(self, payload, *, encrypt=True, sign=False):
        if encrypt:
            body = _callback_body(self.ENCRYPT_KEY, payload)
        else:
            body = json.dumps(payload).encode('utf-8')
        headers = {}
        if sign:
            ts, nonce = str(int(time.time())), f'n-{time.time_ns()}'
            headers = {
                'HTTP_X_LARK_REQUEST_TIMESTAMP': ts,
                'HTTP_X_LARK_REQUEST_NONCE': nonce,
                'HTTP_X_LARK_SIGNATURE': _sign(self.ENCRYPT_KEY, ts, nonce, body),
            }
        return self.client.post(self.app.event_path, data=body,
                                content_type='application/json', **headers)

    def test_unsigned_encrypted_challenge_passes(self):
        """飞书配了 Encrypt Key 时的验证请求：加密、但没有签名头。"""
        resp = self._post({'type': 'url_verification', 'token': self.TOKEN,
                           'challenge': 'chal-abc'})
        self.assertEqual(resp.json()['challenge'], 'chal-abc')

    def test_unsigned_plaintext_challenge_passes(self):
        """飞书侧尚未开加密时的验证请求：明文、也没有签名头。"""
        resp = self._post({'type': 'url_verification', 'token': self.TOKEN,
                           'challenge': 'chal-plain'}, encrypt=False)
        self.assertEqual(resp.json()['challenge'], 'chal-plain')

    def test_unsigned_challenge_still_checks_token(self):
        """放行的只是签名那三道，Verification Token 仍要对。"""
        resp = self._post({'type': 'url_verification', 'token': 'wrong',
                           'challenge': 'chal-x'})
        self.assertEqual(resp.json(), {'code': 0})

    def test_unsigned_real_event_rejected(self):
        """不能让「不带签名」变成绕开验签的通道。"""
        event = {'schema': '2.0',
                 'header': {'token': self.TOKEN, 'event_type': 'im.message.receive_v1'},
                 'event': {}}
        with patch('apps.im_mgr.handlers.handle_event') as handler:
            resp = self._post(event)
        self.assertEqual(resp.json(), {'code': 0})
        handler.assert_not_called()

    def test_signed_plaintext_rejected(self):
        """带签名却没加密：飞书不会这么发，当异常来源处理。"""
        event = {'schema': '2.0',
                 'header': {'token': self.TOKEN, 'event_type': 'im.message.receive_v1'},
                 'event': {}}
        with patch('apps.im_mgr.handlers.handle_event') as handler:
            resp = self._post(event, encrypt=False, sign=True)
        self.assertEqual(resp.json(), {'code': 0})
        handler.assert_not_called()

    def test_signed_event_still_works(self):
        """正式事件推送那条路一道都没少。"""
        event = {'schema': '2.0',
                 'header': {'token': self.TOKEN, 'event_id': 'ev-signed',
                            'event_type': 'im.message.receive_v1'},
                 'event': {}}
        with patch('apps.im_mgr.handlers.handle_event', return_value=None) as handler:
            resp = self._post(event, sign=True)
        self.assertEqual(resp.json(), {'code': 0})
        handler.assert_called_once()


class FeishuErrorDetailTests(TestCase):
    """错误描述的提取：飞书不同接口的错误信封字段名不一样。"""

    def test_prefers_msg(self):
        self.assertEqual(client.error_detail({'code': 1, 'msg': '权限不足'}), '权限不足')

    def test_falls_back_to_oauth_style(self):
        """网页免登那几条走 OAuth 风格信封，描述在 error_description 里。

        只取 msg 的话错误会打成「调用失败：None（code=20024）」——错误码得去翻文档，
        而那句描述本来就写着原因。
        """
        self.assertEqual(
            client.error_detail({'code': 20024, 'error_description': 'code is expired'}),
            'code is expired')
        self.assertEqual(client.error_detail({'code': 20024, 'error': 'invalid_grant'}),
                         'invalid_grant')

    def test_falls_back_to_raw_body(self):
        detail = client.error_detail({'code': 20024, 'msg': ''})
        self.assertIn('20024', detail)

    def test_surfaces_in_exception(self):
        session = FakeSession([TOKEN_OK, {'code': 20024, 'error_description': 'bad code'}])
        app = FeishuApp.load()
        for key, value in FULL_CONFIG.items():
            setattr(app, key, value)
        app.enabled = True
        app.save()
        cache.clear()
        with patch('apps.im_mgr.client.requests.Session', return_value=session):
            with self.assertRaises(client.FeishuError) as ctx:
                client.call('POST', '/open-apis/authen/v1/oidc/access_token')
        self.assertIn('bad code', str(ctx.exception))


class FeishuTokenCacheScopeTests(TestCase):
    """token 缓存必须按 App ID 隔离，改凭据时必须清。

    联调踩到：从个人版应用换成企业版应用后，缓存里还是旧应用的 app_access_token，
    拿它去换新应用产生的授权码，飞书回「The authorization code ... does not match
    the provided application credential」——错误指向授权码，实际问题在凭证。
    """

    def setUp(self):
        cache.clear()
        self.app = FeishuApp.load()
        for key, value in FULL_CONFIG.items():
            setattr(self.app, key, value)
        self.app.enabled = True
        self.app.save()

    def test_cache_key_includes_app_id(self):
        session = FakeSession([TOKEN_OK])
        with patch('apps.im_mgr.client.requests.Session', return_value=session):
            client.get_tenant_token()
        self.assertEqual(cache.get(client.TOKEN_CACHE_PREFIX + 'cli_test0001'), 't-1')

    def test_switching_app_does_not_reuse_old_token(self):
        session = FakeSession([TOKEN_OK])
        with patch('apps.im_mgr.client.requests.Session', return_value=session):
            client.get_tenant_token()

        # 换一个自建应用（个人版 → 企业版就是这么换的）
        FeishuApp.objects.filter(pk=FeishuApp.SINGLETON_PK).update(app_id='cli_other')
        session2 = FakeSession([TOKEN_OK2])
        with patch('apps.im_mgr.client.requests.Session', return_value=session2):
            token = client.get_tenant_token()
        self.assertEqual(token, 't-2')          # 重新取，没吃旧缓存
        self.assertEqual(len(session2.calls), 1)

    def test_drop_token_cache_clears_both(self):
        # 两把 token 是不同接口、不同字段名，队列要各给一条
        app_token_ok = {'code': 0, 'app_access_token': 'a-1', 'expire': 7200}
        session = FakeSession([TOKEN_OK, app_token_ok])
        with patch('apps.im_mgr.client.requests.Session', return_value=session):
            client.get_tenant_token()
            client.get_app_token()
        client.drop_token_cache()
        self.assertIsNone(cache.get(client.TOKEN_CACHE_PREFIX + 'cli_test0001'))
        self.assertIsNone(cache.get(client.APP_TOKEN_CACHE_PREFIX + 'cli_test0001'))

    def test_saving_config_drops_cache(self):
        """App Secret 改了但 App ID 没变时，键是同一个，不清就会用两小时的废 token。"""
        session = FakeSession([TOKEN_OK])
        with patch('apps.im_mgr.client.requests.Session', return_value=session):
            client.get_tenant_token()

        admin = get_user_model().objects.create(username='admin_tok', is_staff=True)
        admin.set_password(PASSWORD)
        admin.save()
        api = APIClient()
        resp = api.post('/api/auth/login/',
                        {'username': 'admin_tok', 'password': PASSWORD}, format='json')
        api.credentials(HTTP_AUTHORIZATION='Bearer ' + resp.json()['access'])
        grant_sudo(api)

        api.put('/api/feishu/app/', {'app_secret': 'rotated-secret'}, format='json')
        self.assertIsNone(cache.get(client.TOKEN_CACHE_PREFIX + 'cli_test0001'))


def _menu_payload(open_id='ou_1', union_id='on_1', event_key='run_task'):
    """机器人自定义菜单事件：没有 chat_id，操作人还多嵌一层 operator_id。"""
    return {
        'schema': '2.0',
        'header': {'event_id': f'menu-{time.time_ns()}',
                   'event_type': 'application.bot.menu_v6'},
        'event': {
            'operator': {'operator_id': {'open_id': open_id, 'union_id': union_id}},
            'event_key': event_key,
            'timestamp': str(int(time.time())),
        },
    }


@override_settings(TASK_EXEC_BACKEND='sync')
class FeishuBotMenuTests(TestCase):
    """机器人自定义菜单 + 单聊发起执行。"""

    def setUp(self):
        from apps.asset_mgr.models import Channel, Environment, ProjectGroup, ServerService, ServiceType
        from apps.asset_mgr.models import Server
        from apps.task_exec.models import ExecutionTask, TaskStep

        cache.clear()
        self.app = FeishuApp.load()
        for key, value in FULL_CONFIG.items():
            setattr(self.app, key, value)
        self.app.enabled = True
        self.app.save()
        pg = ProjectGroup.objects.create(name='主项目', code='m-proj')
        ch = Channel.objects.create(name='官服', code='m-ch')
        env = Environment.objects.create(name='测试环境', code='m-env')
        service = ServiceType.objects.create(name='网关', code='m-gw')
        server = Server.objects.create(name='web1', private_ip='10.0.0.1',
                                       project_group=pg, channel=ch, environment=env)
        ServerService.objects.create(server=server, service_type=service, status='planned')
        self.task = ExecutionTask.objects.create(
            name='重启网关', project_group=pg, channel=ch, environment=env,
            im_enabled=True, default_select_all=True)
        TaskStep.objects.create(task=self.task, order=1, name='重启',
                                command='echo hi', service_type=service)
        self.user = get_user_model().objects.create(username='menu_ops', is_staff=True)
        FeishuBinding.objects.create(user=self.user, union_id='on_1', open_id='ou_1')

    def _enable_p2p(self):
        FeishuApp.objects.filter(pk=FeishuApp.SINGLETON_PK).update(allow_p2p_execute=True)

    # ── payload 解析 ──────────────────────────────────────
    def test_operator_id_is_nested_one_level_deeper(self):
        """照着卡片回调的路径取会拿到空值，表现成「账号未绑定」，指向完全错误的方向。"""
        self.assertEqual(handlers.operator_ids(_menu_payload()), ('ou_1', 'on_1'))

    def test_menu_counts_as_p2p(self):
        self.assertTrue(handlers.is_p2p(_menu_payload()))

    # ── 菜单点击 ──────────────────────────────────────────
    def test_menu_replies_privately(self):
        """菜单不属于任何会话，只能按 open_id 私聊回。"""
        self._enable_p2p()
        with patch('apps.im_mgr.tasks.dispatch_send_card') as send:
            handlers.handle_event(self.app, _menu_payload())
        receive_id, card = send.call_args[0][:2]
        self.assertEqual(receive_id, 'ou_1')
        self.assertEqual(send.call_args[1]['receive_id_type'], 'open_id')
        self.assertIn('重启网关', ''.join(_texts(card)))

    def test_menu_blocked_when_p2p_disabled(self):
        """默认不开单聊执行：说清原因，不要给空下拉。"""
        with patch('apps.im_mgr.tasks.dispatch_send_card') as send:
            handlers.handle_event(self.app, _menu_payload())
        text = ''.join(_texts(send.call_args[0][1]))
        self.assertIn('单聊尚未开放', text)

    def test_menu_from_unbound_account(self):
        self._enable_p2p()
        with patch('apps.im_mgr.tasks.dispatch_send_card') as send:
            handlers.handle_event(self.app, _menu_payload(open_id='ou_x', union_id='on_x'))
        self.assertIn('还没有绑定平台用户', ''.join(_texts(send.call_args[0][1])))

    # ── 单聊消息 ──────────────────────────────────────────
    def test_p2p_message_needs_switch(self):
        payload = _event_payload(chat_id='oc_p2p')
        payload['event']['message']['chat_type'] = 'p2p'
        with patch('apps.im_mgr.tasks.dispatch_send_card') as send:
            handlers.handle_event(self.app, payload)
        self.assertIn('单聊尚未开放', ''.join(_texts(send.call_args[0][1])))

        self._enable_p2p()
        with patch('apps.im_mgr.tasks.dispatch_send_card') as send:
            handlers.handle_event(self.app, payload)
        self.assertIn('重启网关', ''.join(_texts(send.call_args[0][1])))

    # ── 单聊里点执行 ──────────────────────────────────────
    def test_p2p_card_run_blocked_when_disabled(self):
        payload = _card_payload({'act': cards.ACT_RUN, 'task': self.task.id}, chat_id='')
        with patch('apps.task_exec.tasks.execute_task') as runner:
            resp = handlers.handle_card_action(self.app, payload)
        runner.assert_not_called()
        self.assertIn('单聊', ''.join(_texts(resp['card']['data'])))

    def test_p2p_card_run_allowed_when_enabled(self):
        self._enable_p2p()
        payload = _card_payload({'act': cards.ACT_RUN, 'task': self.task.id}, chat_id='')
        with patch('apps.task_exec.tasks.execute_task') as runner:
            resp = handlers.handle_card_action(self.app, payload)
        runner.assert_called_once()
        self.assertEqual(resp['toast']['type'], 'success')

    def test_group_still_uses_whitelist(self):
        """开了单聊开关不等于群里也放开。"""
        self._enable_p2p()
        payload = _card_payload({'act': cards.ACT_RUN, 'task': self.task.id},
                                chat_id='oc_unlisted')
        with patch('apps.task_exec.tasks.execute_task') as runner:
            resp = handlers.handle_card_action(self.app, payload)
        runner.assert_not_called()
        self.assertIn('未被授权', ''.join(_texts(resp['card']['data'])))


class FeishuTimestampParseTests(TestCase):
    """时间戳解析：事件订阅给 Unix 秒，卡片回调给 Go 的 time.Time 字符串。

    联调踩到：卡片按钮全被拒，日志是
    `时间戳非法：'2026-09-16 17:22:51.152344108 +0800 CST m=+526362.485448901'`。
    签名那道是过的——签名用的是原样字符串，只有解析这一步认死了数字。
    """

    def test_unix_seconds(self):
        self.assertEqual(crypto.parse_timestamp('1758014571'), 1758014571)

    def test_unix_millis(self):
        """有的接口给毫秒；按量级判，别让「1970 年」的偏差把请求拒掉。"""
        self.assertEqual(crypto.parse_timestamp('1758014571000'), 1758014571)

    def test_go_time_string(self):
        parsed = crypto.parse_timestamp(
            '2026-09-16 17:22:51.152344108 +0800 CST m=+526362.485448901')
        expected = int(datetime(2026, 9, 16, 17, 22, 51,
                                tzinfo=timezone(timedelta(hours=8))).timestamp())
        self.assertEqual(parsed, expected)

    def test_go_time_without_nanos(self):
        self.assertEqual(
            crypto.parse_timestamp('2026-09-16 17:22:51 +0800 CST'),
            crypto.parse_timestamp('2026-09-16 17:22:51.999 +0800 CST'))

    def test_rejects_garbage(self):
        for bad in ('', 'not-a-time', '2026/09/16 17:22:51'):
            with self.assertRaises(crypto.CallbackError):
                crypto.parse_timestamp(bad)

    def test_window_still_enforced_for_go_format(self):
        """能解析了，但过期的照样拒。"""
        stale = datetime.now(timezone(timedelta(hours=8))) - timedelta(seconds=600)
        text = stale.strftime('%Y-%m-%d %H:%M:%S.000000000 %z CST m=+1.0')
        with self.assertRaises(crypto.CallbackError):
            crypto.verify_timestamp(text)

    def test_card_callback_with_go_timestamp_passes(self):
        """端到端：带 Go 格式时间戳的卡片回调应当被接受。"""
        app = FeishuApp.load()
        for key, value in FULL_CONFIG.items():
            setattr(app, key, value)
        app.ip_whitelist = '127.0.0.1'
        app.enabled = True
        app.save()
        cache.clear()

        payload = {'schema': '2.0',
                   'header': {'token': 'token-value', 'event_id': 'ca-go',
                              'event_type': 'card.action.trigger'},
                   'event': {'operator': {'open_id': 'ou_1'},
                             'context': {'open_chat_id': 'oc_1', 'open_message_id': 'om_1'},
                             'action': {'tag': 'button', 'value': {'act': 'cancel'}}}}
        body = _callback_body('encrypt-value', payload)
        ts = datetime.now(timezone(timedelta(hours=8))).strftime(
            '%Y-%m-%d %H:%M:%S.123456789 %z CST m=+1.5')
        nonce = f'n-{time.time_ns()}'
        resp = APIClient().post(
            app.card_path, data=body, content_type='application/json',
            HTTP_X_LARK_REQUEST_TIMESTAMP=ts,
            HTTP_X_LARK_REQUEST_NONCE=nonce,
            HTTP_X_LARK_SIGNATURE=_sign('encrypt-value', ts, nonce, body))
        # cancel 动作会回一张「已取消」的定格卡
        self.assertIn('已取消', ''.join(_texts(resp.json()['card']['data'])))


@override_settings(TASK_EXEC_BACKEND='sync')
class FeishuP2pScopeTests(TestCase):
    """会话场景必须随按钮 value 带回来。

    联调踩到：菜单私聊唤出任务列表、点执行却报「当前会话未被授权发起任务执行」
    ——那是**群聊**分支的文案。原因是用 chat_id 前缀猜场景，而**单聊的 chat_id
    同样是 oc_ 开头**（飞书没有 p2p_ 这种前缀），单聊卡片全被当成未授权的群。
    """

    def setUp(self):
        from apps.asset_mgr.models import Channel, Environment, ProjectGroup, ServerService, ServiceType
        from apps.asset_mgr.models import Server
        from apps.task_exec.models import ExecutionTask, TaskStep

        cache.clear()
        self.app = FeishuApp.load()
        for key, value in FULL_CONFIG.items():
            setattr(self.app, key, value)
        self.app.enabled = True
        self.app.allow_p2p_execute = True
        self.app.save()
        pg = ProjectGroup.objects.create(name='主项目', code='s-proj')
        ch = Channel.objects.create(name='官服', code='s-ch')
        env = Environment.objects.create(name='测试环境', code='s-env')
        service = ServiceType.objects.create(name='网关', code='s-gw')
        server = Server.objects.create(name='web1', private_ip='10.0.0.1',
                                       project_group=pg, channel=ch, environment=env)
        ServerService.objects.create(server=server, service_type=service, status='planned')
        self.task = ExecutionTask.objects.create(
            name='重启网关', project_group=pg, channel=ch, environment=env,
            im_enabled=True, default_select_all=True)
        TaskStep.objects.create(task=self.task, order=1, name='重启',
                                command='echo hi', service_type=service)
        self.user = get_user_model().objects.create(username='scope_ops', is_staff=True)
        FeishuBinding.objects.create(user=self.user, union_id='on_1', open_id='ou_1')

    def test_picker_carries_scope(self):
        card = cards.task_picker([self.task], p2p=True)
        select = card['body']['elements'][1]
        self.assertTrue(_bval(select)['p2p'])

    def test_run_button_carries_scope(self):
        card = cards.task_detail(self.task, server_count=1, step_count=1, p2p=True)
        self.assertTrue(_bval(_buttons(card)[0])['p2p'])

    def test_p2p_run_works_with_oc_chat_id(self):
        """单聊的 chat_id 也是 oc_ 开头：靠 value 里的标记，不靠前缀猜。"""
        payload = _card_payload({'act': cards.ACT_RUN, 'task': self.task.id, 'p2p': True},
                                chat_id='oc_p2p_looks_like_group')
        with patch('apps.task_exec.tasks.execute_task') as runner:
            resp = handlers.handle_card_action(self.app, payload)
        runner.assert_called_once()
        self.assertEqual(resp['toast']['type'], 'success')

    def test_group_card_without_scope_still_uses_whitelist(self):
        payload = _card_payload({'act': cards.ACT_RUN, 'task': self.task.id, 'p2p': False},
                                chat_id='oc_unlisted')
        with patch('apps.task_exec.tasks.execute_task') as runner:
            resp = handlers.handle_card_action(self.app, payload)
        runner.assert_not_called()
        self.assertIn('未被授权', ''.join(_texts(resp['card']['data'])))

    def test_pick_passes_scope_to_detail_card(self):
        payload = _card_payload({'act': cards.ACT_PICK_TASK, 'p2p': True},
                                chat_id='oc_p2p_looks_like_group',
                                option=str(self.task.id))
        resp = handlers.handle_card_action(self.app, payload)
        self.assertTrue(_bval(_buttons(resp['card']['data'])[0])['p2p'])


@override_settings(TASK_EXEC_BACKEND='sync')
class FeishuHostSelectTests(TestCase):
    """飞书侧勾选执行主机：默认全选，需要时再改。"""

    def setUp(self):
        from apps.asset_mgr.models import Channel, Environment, ProjectGroup, ServerService, ServiceType
        from apps.asset_mgr.models import Server
        from apps.task_exec.models import ExecutionTask, TaskStep

        cache.clear()
        self.app = FeishuApp.load()
        for key, value in FULL_CONFIG.items():
            setattr(self.app, key, value)
        self.app.enabled = True
        self.app.save()
        self.pg = ProjectGroup.objects.create(name='主项目', code='h-proj')
        self.ch = Channel.objects.create(name='官服', code='h-ch')
        self.env = Environment.objects.create(name='测试环境', code='h-env')
        self.prod = Environment.objects.create(name='生产环境', code='h-prod', is_prod=True)
        self.service = ServiceType.objects.create(name='网关', code='h-gw')
        self.servers = []
        for i in range(1, 4):
            server = Server.objects.create(
                name=f'web{i}', private_ip=f'10.0.0.{i}',
                project_group=self.pg, channel=self.ch, environment=self.env)
            ServerService.objects.create(server=server, service_type=self.service,
                                         status='planned')
            self.servers.append(server)
        self.task = ExecutionTask.objects.create(
            name='重启网关', project_group=self.pg, channel=self.ch, environment=self.env,
            im_enabled=True, default_select_all=True)
        TaskStep.objects.create(task=self.task, order=1, name='重启',
                                command='echo hi', service_type=self.service)
        FeishuChannel.objects.create(chat_id='oc_ops', name='运维群', allow_execute=True)
        self.user = get_user_model().objects.create(username='hs_ops', is_staff=True)
        FeishuBinding.objects.create(user=self.user, union_id='on_1', open_id='ou_1')

    def _run(self, payload):
        with patch('apps.task_exec.tasks.execute_task') as runner:
            resp = handlers.handle_card_action(self.app, payload)
        return resp, runner

    def _form_payload(self, hosts, *, task=None, chat_id='oc_ops'):
        payload = _card_payload(
            {'act': cards.ACT_RUN_SELECTED, 'task': (task or self.task).id, 'p2p': False},
            chat_id=chat_id)
        payload['event']['action']['form_value'] = {cards.HOSTS_FIELD: hosts}
        return payload

    # ── 容错解析 ──────────────────────────────────────────
    def test_parses_every_shape_feishu_might_send(self):
        """判错的后果是「明明勾了却按全量执行」，生产上不可接受。"""
        shapes = [
            ['1', '2'],
            [1, 2],
            '1,2',
            ' 1 , 2 ',
            [{'value': '1'}, {'value': '2'}],
        ]
        for raw in shapes:
            payload = self._form_payload(raw)
            self.assertEqual(handlers.selected_host_ids(payload), ['1', '2'], raw)

    def test_parses_form_value_without_field_name(self):
        payload = self._form_payload(None)
        payload['event']['action']['form_value'] = ['7', '8']
        self.assertEqual(handlers.selected_host_ids(payload), ['7', '8'])

    def test_no_form_value_means_empty(self):
        payload = _card_payload({'act': cards.ACT_RUN_SELECTED, 'task': self.task.id})
        self.assertEqual(handlers.selected_host_ids(payload), [])

    # ── 确认卡上的入口 ────────────────────────────────────
    def test_detail_offers_host_picker(self):
        card = cards.task_detail(self.task, server_count=3, step_count=1)
        acts = [_bval(b)['act'] for b in _buttons(card)]
        self.assertIn(cards.ACT_PICK_HOSTS, acts)
        self.assertIn('全部 3 台', ''.join(_texts(card)))

    def test_detail_hides_picker_for_single_host(self):
        card = cards.task_detail(self.task, server_count=1, step_count=1)
        acts = [_bval(b)['act'] for b in _buttons(card)]
        self.assertNotIn(cards.ACT_PICK_HOSTS, acts)

    def test_detail_warns_when_too_many_hosts(self):
        card = cards.task_detail(self.task, server_count=200, step_count=1)
        self.assertIn(f'超过 {cards.MAX_HOSTS_SELECTABLE} 台', ''.join(_texts(card)))

    # ── 多选卡 ────────────────────────────────────────────
    def test_pick_hosts_returns_form_card_all_selected(self):
        payload = _card_payload({'act': cards.ACT_PICK_HOSTS, 'task': self.task.id})
        resp, _ = self._run(payload)
        card = resp['card']['data']
        self.assertEqual(card['schema'], '2.0')
        select = card['body']['elements'][1]['elements'][0]
        self.assertEqual(select['tag'], 'multi_select_static')
        self.assertEqual(len(select['options']), 3)
        # 默认全勾；initial_options 会被飞书严格校验拒掉，只能用 selected_values
        self.assertEqual(sorted(select['selected_values']),
                         sorted(str(s.id) for s in self.servers))
        self.assertNotIn('initial_options', select)

    def test_pick_hosts_rechecks_access(self):
        self.task.im_enabled = False
        self.task.save(update_fields=['im_enabled'])
        payload = _card_payload({'act': cards.ACT_PICK_HOSTS, 'task': self.task.id})
        resp, _ = self._run(payload)
        self.assertIn('未开启飞书执行', ''.join(_texts(resp['card']['data'])))

    # ── 按勾选执行 ────────────────────────────────────────
    def test_runs_only_selected_hosts(self):
        from apps.task_exec.models import TaskExecutionLog

        keep = [self.servers[0], self.servers[2]]
        resp, runner = self._run(self._form_payload([str(s.id) for s in keep]))
        runner.assert_called_once()
        log = TaskExecutionLog.objects.get(task=self.task)
        self.assertEqual({row['name'] for row in log.servers_snapshot},
                         {'web1', 'web3'})
        self.assertEqual(resp['toast']['type'], 'success')

    def test_empty_selection_rejected(self):
        """一台都没勾却提交，不能悄悄退回全量执行。"""
        resp, runner = self._run(self._form_payload(['999999']))
        runner.assert_not_called()
        self.assertIn('已不在当前范围', ''.join(_texts(resp['card']['data'])))

    def test_stale_host_rejected(self):
        """卡片可能是几分钟前发的，期间主机可能下线或改了维度。"""
        gone = self.servers[1]
        ids = [str(s.id) for s in self.servers]
        gone.environment = self.prod
        gone.save(update_fields=['environment'])
        resp, runner = self._run(self._form_payload(ids))
        runner.assert_not_called()
        self.assertIn('已不在当前范围', ''.join(_texts(resp['card']['data'])))

    def test_plain_run_still_uses_all_hosts(self):
        """默认路径一步没多：不点「选择主机」就是全部执行。"""
        from apps.task_exec.models import TaskExecutionLog

        resp, runner = self._run(
            _card_payload({'act': cards.ACT_RUN, 'task': self.task.id}))
        runner.assert_called_once()
        log = TaskExecutionLog.objects.get(task=self.task)
        self.assertEqual(len(log.servers_snapshot), 3)

    # ── 生产：选择 → 确认 ─────────────────────────────────
    def _to_prod(self):
        from apps.asset_mgr.models import ServerService

        for server in self.servers:
            server.environment = self.prod
            server.save(update_fields=['environment'])
        ServerService.objects.filter(server__in=self.servers).update(status='planned')
        self.task.environment = self.prod
        self.task.save(update_fields=['environment'])

    def test_prod_selection_rides_on_the_token(self):
        """选中的主机存服务端：卡片上的值只用于展示。"""
        self._to_prod()
        keep = [self.servers[0]]
        resp, runner = self._run(self._form_payload([str(s.id) for s in keep]))
        runner.assert_not_called()
        token = FeishuConfirmToken.objects.get()
        self.assertEqual(token.server_ids, [keep[0].id])
        self.assertIn('1 台主机', ''.join(_texts(resp['card']['data'])))

    def test_prod_confirm_runs_only_selected(self):
        from apps.task_exec.models import TaskExecutionLog

        self._to_prod()
        self._run(self._form_payload([str(self.servers[0].id)]))
        token = FeishuConfirmToken.objects.get().token
        resp, runner = self._run(
            _card_payload({'act': cards.ACT_CONFIRM_PROD, 'token': token}))
        runner.assert_called_once()
        log = TaskExecutionLog.objects.get(task=self.task)
        self.assertEqual([row['name'] for row in log.servers_snapshot], ['web1'])
        self.assertEqual(resp['toast']['type'], 'success')

    def test_prod_confirm_without_selection_runs_all(self):
        """没经过勾选的生产执行，票据里 server_ids 为空 = 全部。"""
        from apps.task_exec.models import TaskExecutionLog

        self._to_prod()
        self._run(_card_payload({'act': cards.ACT_RUN, 'task': self.task.id}))
        token = FeishuConfirmToken.objects.get().token
        self._run(_card_payload({'act': cards.ACT_CONFIRM_PROD, 'token': token}))
        log = TaskExecutionLog.objects.get(task=self.task)
        self.assertEqual(len(log.servers_snapshot), 3)


@override_settings(TASK_EXEC_BACKEND='sync')
class FeishuServiceTypeScopeTests(TestCase):
    """卡片上的主机必须按**模块服务类型**收窄。

    联调发现：多选里混进了根本不会被任何模块执行的主机——因为
    `task.matched_servers()` 只按三维度匹配，而服务类型是模块级的。
    勾了不生效，还让人以为会在上面跑。
    """

    def setUp(self):
        from apps.asset_mgr.models import Channel, Environment, ProjectGroup, ServerService, ServiceType
        from apps.asset_mgr.models import Server
        from apps.task_exec.models import ExecutionTask, TaskStep

        cache.clear()
        self.app = FeishuApp.load()
        for key, value in FULL_CONFIG.items():
            setattr(self.app, key, value)
        self.app.enabled = True
        self.app.save()
        self.pg = ProjectGroup.objects.create(name='主项目', code='st-proj')
        self.ch = Channel.objects.create(name='官服', code='st-ch')
        self.env = Environment.objects.create(name='测试环境', code='st-env')
        self.gateway = ServiceType.objects.create(name='网关', code='st-gw')
        self.db = ServiceType.objects.create(name='数据库', code='st-db')

        def server(name, ip, service):
            obj = Server.objects.create(name=name, private_ip=ip, project_group=self.pg,
                                        channel=self.ch, environment=self.env)
            ServerService.objects.create(server=obj, service_type=service, status='planned')
            return obj

        self.gw1 = server('gw1', '10.0.0.1', self.gateway)
        self.gw2 = server('gw2', '10.0.0.2', self.gateway)
        self.db1 = server('db1', '10.0.0.3', self.db)   # 任务里没有模块用得上它

        self.task = ExecutionTask.objects.create(
            name='重启网关', project_group=self.pg, channel=self.ch, environment=self.env,
            im_enabled=True, default_select_all=True)
        TaskStep.objects.create(task=self.task, order=1, name='重启',
                                command='echo hi', service_type=self.gateway)
        FeishuChannel.objects.create(chat_id='oc_ops', name='运维群', allow_execute=True)
        self.user = get_user_model().objects.create(username='st_ops', is_staff=True)
        FeishuBinding.objects.create(user=self.user, union_id='on_1', open_id='ou_1')

    def _run(self, payload):
        with patch('apps.task_exec.tasks.execute_task') as runner:
            resp = handlers.handle_card_action(self.app, payload)
        return resp, runner

    def test_three_dimensions_alone_are_too_wide(self):
        """三维度匹配到 3 台，但只有 2 台是网关。"""
        self.assertEqual(self.task.matched_servers().count(), 3)
        names = {s.name for s in access.selectable_servers(self.task)}
        self.assertEqual(names, {'gw1', 'gw2'})

    def test_multi_select_lists_only_relevant_hosts(self):
        payload = _card_payload({'act': cards.ACT_PICK_HOSTS, 'task': self.task.id})
        resp, _ = self._run(payload)
        select = resp['card']['data']['body']['elements'][1]['elements'][0]
        labels = [o['text']['content'] for o in select['options']]
        self.assertEqual(len(labels), 2)
        self.assertFalse(any('db1' in label for label in labels))

    def test_detail_count_matches_the_picker(self):
        """两处对不上的话，「当前匹配 3 台」点进去只有 2 台，更让人困惑。"""
        payload = _card_payload({'act': cards.ACT_PICK_TASK}, option=str(self.task.id))
        resp, _ = self._run(payload)
        self.assertIn('当前匹配 2 台', ''.join(_texts(resp['card']['data'])))

    def test_picking_an_irrelevant_host_is_rejected(self):
        payload = _card_payload(
            {'act': cards.ACT_RUN_SELECTED, 'task': self.task.id, 'p2p': False})
        payload['event']['action']['form_value'] = {cards.HOSTS_FIELD: [str(self.db1.id)]}
        resp, runner = self._run(payload)
        runner.assert_not_called()
        self.assertIn('已不在当前范围', ''.join(_texts(resp['card']['data'])))

    def test_run_all_skips_irrelevant_host(self):
        from apps.task_exec.models import TaskExecutionLog

        resp, runner = self._run(_card_payload({'act': cards.ACT_RUN, 'task': self.task.id}))
        runner.assert_called_once()
        log = TaskExecutionLog.objects.get(task=self.task)
        self.assertEqual({row['name'] for row in log.servers_snapshot}, {'gw1', 'gw2'})

    def test_multiple_step_types_take_the_union(self):
        """两个模块分别面向网关与数据库时，并集里两种都有。"""
        from apps.task_exec.models import TaskStep

        TaskStep.objects.create(task=self.task, order=2, name='备份',
                                command='echo db', service_type=self.db)
        names = {s.name for s in access.selectable_servers(self.task)}
        self.assertEqual(names, {'gw1', 'gw2', 'db1'})

    def test_custom_scope_steps_do_not_offer_selection(self):
        """自定义范围的模块按自己的维度实时匹配，压根不吃勾选。"""
        from apps.task_exec.models import TaskStep

        TaskStep.objects.filter(task=self.task).update(
            use_custom_scope=True, project_group=self.pg, channel=self.ch,
            environment=self.env)
        self.assertIsNone(access.selectable_servers(self.task))
        _ = TaskStep


@override_settings(TASK_EXEC_BACKEND='sync', SITE_BASE_URL='https://ops.example.com')
class FeishuBroadcastTests(TestCase):
    """群订阅播报：失败自动播到订阅了的会话。"""

    def setUp(self):
        from apps.asset_mgr.models import Channel, Environment, ProjectGroup
        from apps.task_exec.models import ExecutionTask, TaskExecutionLog

        cache.clear()
        self.app = FeishuApp.load()
        for key, value in FULL_CONFIG.items():
            setattr(self.app, key, value)
        self.app.enabled = True
        self.app.save()
        self.pg = ProjectGroup.objects.create(name='主项目', code='bc-proj')
        self.other_pg = ProjectGroup.objects.create(name='别的项目', code='bc-other')
        self.ch = Channel.objects.create(name='官服', code='bc-ch')
        self.env = Environment.objects.create(name='测试环境', code='bc-env')
        self.prod = Environment.objects.create(name='生产环境', code='bc-prod', is_prod=True)
        self.task = ExecutionTask.objects.create(
            name='重启网关', project_group=self.pg, channel=self.ch, environment=self.env,
            im_enabled=True)
        self.log = TaskExecutionLog.objects.create(
            task=self.task, status='failed', trigger_type='execute',
            output='fatal: 连接超时',
            steps_result=[{'order': 1, 'name': '重启', 'status': 'failed'}])
        self.group = FeishuChannel.objects.create(
            chat_id='oc_ops', name='运维群', notify_enabled=True)

    def _broadcast(self):
        with patch('apps.im_mgr.client.send_card') as send:
            notify.broadcast(self.log.id)
        return send

    # ── 基本行为 ──────────────────────────────────────────
    def test_failure_is_broadcast(self):
        send = self._broadcast()
        chat_id, card = send.call_args[0][:2]
        self.assertEqual(chat_id, 'oc_ops')
        text = ''.join(_texts(card))
        self.assertIn('重启网关', text)
        self.assertIn('连接超时', text)   # 失败附日志尾段，不用开页面就能判断

    def test_success_not_broadcast_by_default(self):
        """成功的执行每天几十上百次，全播等于把群变成噪声源。"""
        self.log.status = 'success'
        self.log.save(update_fields=['status'])
        self._broadcast().assert_not_called()

    def test_success_broadcast_when_scope_is_all(self):
        FeishuChannel.objects.filter(pk=self.group.pk).update(notify_scope='all')
        self.log.status = 'success'
        self.log.save(update_fields=['status'])
        self._broadcast().assert_called_once()

    def test_notify_disabled_channel_skipped(self):
        FeishuChannel.objects.filter(pk=self.group.pk).update(notify_enabled=False)
        self._broadcast().assert_not_called()

    def test_disabled_channel_skipped(self):
        FeishuChannel.objects.filter(pk=self.group.pk).update(enabled=False)
        self._broadcast().assert_not_called()

    def test_running_log_not_broadcast(self):
        self.log.status = 'running'
        self.log.save(update_fields=['status'])
        self._broadcast().assert_not_called()

    # ── 维度订阅 ──────────────────────────────────────────
    def test_dimension_filter_excludes_other_projects(self):
        """一个群只关心自己那摊事，别的项目组的失败播过来只会被当噪声。"""
        FeishuChannel.objects.filter(pk=self.group.pk).update(
            notify_project_group=self.other_pg)
        self._broadcast().assert_not_called()

    def test_dimension_filter_matches(self):
        FeishuChannel.objects.filter(pk=self.group.pk).update(
            notify_project_group=self.pg, notify_environment=self.env)
        self._broadcast().assert_called_once()

    def test_blank_dimension_means_all(self):
        self.assertIsNone(self.group.notify_project_group_id)
        self._broadcast().assert_called_once()

    # ── 去重与免重复 ──────────────────────────────────────
    def test_broadcast_only_once(self):
        """终态可能被重复广播（状态推送 + 模块表写入各来一次）。"""
        with patch('apps.im_mgr.client.send_card') as send:
            notify.broadcast(self.log.id)
            notify.broadcast(self.log.id)
        self.assertEqual(send.call_count, 1)

    def test_skips_the_chat_that_already_has_the_card(self):
        """从飞书发起的执行，那张卡已经在群里刷成终态了，再播一条是纯噪声。"""
        notify.bind(self.log, 'oc_ops', 'om_1')
        self._broadcast().assert_not_called()

    def test_still_broadcasts_to_other_chats(self):
        FeishuChannel.objects.create(chat_id='oc_other', name='值班群', notify_enabled=True)
        notify.bind(self.log, 'oc_ops', 'om_1')
        send = self._broadcast()
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args[0][0], 'oc_other')

    def test_cache_failure_still_broadcasts(self):
        """漏播的代价是没人知道生产挂了——宁可多播一条。"""
        with patch('apps.im_mgr.notify.cache.add', side_effect=RuntimeError('down')), \
                patch('apps.im_mgr.client.send_card') as send:
            notify.broadcast(self.log.id)
        send.assert_called_once()

    # ── 与执行链路对接 ────────────────────────────────────
    def test_terminal_status_triggers_broadcast_without_card(self):
        """页面发起的执行没有卡片，但它的失败同样该让值班的人看见。"""
        with patch('apps.im_mgr.tasks.dispatch_broadcast') as job:
            notify.on_execution_updated(None, task_id=self.task.id, log_id=self.log.id)
        job.assert_called_once_with(self.log.id)

    def test_feishu_failure_does_not_escape(self):
        with patch('apps.im_mgr.client.send_card',
                   side_effect=client.FeishuError('bot not in chat')):
            tasks.broadcast(self.log.id)
        self.log.refresh_from_db()
        self.assertEqual(self.log.status, 'failed')
