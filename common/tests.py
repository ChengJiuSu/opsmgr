"""common 工具测试：TOTP（RFC 6238 官方向量）、Fernet 加解密、限速、鉴权用 IP 解析、结构化日志。"""
import base64
import json
import logging
import time
from types import SimpleNamespace
from unittest import mock

from django.core.cache import cache
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings

from . import crypto, fields, ratelimit, totp
from . import logging as log_utils
from . import request as request_utils

# RFC 6238 附录 B 的 SHA1 测试向量（密钥 "12345678901234567890"）
RFC_SECRET = base64.b32encode(b'12345678901234567890').decode('ascii')
RFC_VECTORS = [
    (59, '287082'),
    (1111111109, '081804'),
    (1111111111, '050471'),
    (1234567890, '005924'),
    (2000000000, '279037'),
    (20000000000, '353130'),
]


class TotpTests(SimpleTestCase):
    def test_rfc6238_vectors(self):
        for timestamp, expected in RFC_VECTORS:
            self.assertEqual(totp.code_at(RFC_SECRET, timestamp // totp.PERIOD), expected)

    def test_random_secret_is_base32_and_unique(self):
        first, second = totp.random_secret(), totp.random_secret()
        self.assertNotEqual(first, second)
        self.assertEqual(len(first), 32)  # 20 字节 → 32 个 base32 字符
        base64.b32decode(totp.normalize_secret(first))

    def test_normalize_accepts_spaces_and_lowercase(self):
        secret = totp.random_secret()
        spaced = ' '.join(secret[i:i + 4] for i in range(0, len(secret), 4)).lower()
        self.assertEqual(totp.code_at(spaced, 100), totp.code_at(secret, 100))

    def test_verify_current_window(self):
        now = time.time()
        code = totp.code_at(RFC_SECRET, totp.current_counter(now))
        self.assertEqual(totp.verify(RFC_SECRET, code, at=now), totp.current_counter(now))

    def test_verify_tolerates_one_window_drift(self):
        now = time.time()
        base = totp.current_counter(now)
        for offset in (-1, 1):
            code = totp.code_at(RFC_SECRET, base + offset)
            self.assertEqual(totp.verify(RFC_SECRET, code, at=now), base + offset)
        # 两个窗口之外不认（超过 ±30s 就该让用户重新读码）
        self.assertIsNone(
            totp.verify(RFC_SECRET, totp.code_at(RFC_SECRET, base + 2), at=now))

    def test_verify_rejects_malformed(self):
        for bad in ('', None, 'abcdef', '12345', '1234567'):
            self.assertIsNone(totp.verify(RFC_SECRET, bad))

    def test_after_counter_blocks_replay(self):
        now = time.time()
        counter = totp.current_counter(now)
        code = totp.code_at(RFC_SECRET, counter)
        self.assertIsNone(
            totp.verify(RFC_SECRET, code, at=now, after_counter=counter))

    def test_provisioning_uri(self):
        uri = totp.provisioning_uri(RFC_SECRET, 'alice@corp', 'OpsManager')
        self.assertTrue(uri.startswith('otpauth://totp/OpsManager%3Aalice%40corp?'))
        self.assertIn('issuer=OpsManager', uri)
        self.assertIn('period=30', uri)
        self.assertIn('digits=6', uri)


class CryptoTests(SimpleTestCase):
    def test_roundtrip(self):
        self.assertEqual(crypto.decrypt(crypto.encrypt('s3cret')), 's3cret')

    def test_ciphertext_differs_each_time(self):
        """Fernet 带随机 IV：同一明文两次加密结果不同，库里看不出谁和谁一样。"""
        self.assertNotEqual(crypto.encrypt('same'), crypto.encrypt('same'))

    def test_empty_passthrough(self):
        self.assertEqual(crypto.encrypt(''), '')
        self.assertEqual(crypto.decrypt(''), '')

    def test_key_change_breaks_decrypt(self):
        token = crypto.encrypt('s3cret')
        # 必须覆盖优先级最高的 SECRET_ENC_KEY：密钥按
        # SECRET_ENC_KEY → MFA_SECRET_KEY → SECRET_KEY 回退，只改 MFA_SECRET_KEY
        # 的话，凡是配了 SECRET_ENC_KEY 的环境（部署用的 .env 里就有，而
        # settings 的 config() 会读它）根本没换到密钥，解密照样成功。
        with override_settings(SECRET_ENC_KEY='another-key-entirely'):
            with self.assertRaises(crypto.DecryptError):
                crypto.decrypt(token)


class RateLimitTests(TestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_blocks_after_limit(self):
        for _ in range(2):
            self.assertEqual(ratelimit.record_failure('t', 'a', 3, 60, 120), 0)
        self.assertEqual(ratelimit.record_failure('t', 'a', 3, 60, 120), 120)
        self.assertGreater(ratelimit.blocked_seconds('t', 'a'), 0)

    def test_scopes_and_idents_isolated(self):
        for _ in range(3):
            ratelimit.record_failure('t', 'a', 3, 60, 120)
        self.assertEqual(ratelimit.blocked_seconds('t', 'b'), 0)
        self.assertEqual(ratelimit.blocked_seconds('other', 'a'), 0)

    def test_reset_clears(self):
        for _ in range(3):
            ratelimit.record_failure('t', 'a', 3, 60, 120)
        ratelimit.reset('t', 'a')
        self.assertEqual(ratelimit.blocked_seconds('t', 'a'), 0)

    def test_count_restarts_after_block(self):
        """锁定时清计数：否则解锁后差一次失败就又被锁。"""
        for _ in range(3):
            ratelimit.record_failure('t', 'a', 3, 60, 120)
        ratelimit.reset('t', 'a')
        self.assertEqual(ratelimit.record_failure('t', 'a', 3, 60, 120), 0)

    def test_cache_outage_fails_open(self):
        with mock.patch('common.ratelimit.cache') as fake:
            fake.add.side_effect = RuntimeError('down')
            fake.get.side_effect = RuntimeError('down')
            self.assertEqual(ratelimit.record_failure('t', 'a', 1, 60, 120), 0)
            self.assertEqual(ratelimit.blocked_seconds('t', 'a'), 0)

    def test_ident_not_stored_in_plaintext_key(self):
        ratelimit.record_failure('t', 'alice', 3, 60, 120)
        self.assertNotIn('alice', ''.join(getattr(cache, '_cache', {}).keys()))


@override_settings(TRUSTED_PROXIES=['127.0.0.1', '10.8.0.0/24'])
class AuthClientIpTests(SimpleTestCase):
    """鉴权用 IP 解析：只信可信代理转发的 XFF，伪造头必须无效。"""

    @staticmethod
    def _request(remote, forwarded=None):
        meta = {'REMOTE_ADDR': remote}
        if forwarded is not None:
            meta['HTTP_X_FORWARDED_FOR'] = forwarded
        return SimpleNamespace(META=meta)

    def test_direct_connection_uses_remote_addr(self):
        req = self._request('203.0.113.9')
        self.assertEqual(request_utils.auth_client_ip(req), '203.0.113.9')

    def test_forged_xff_ignored_when_peer_untrusted(self):
        """核心用例：直连时客户端自称内网 IP 不能生效，否则白名单等于没有。"""
        req = self._request('203.0.113.9', '10.0.0.5')
        self.assertEqual(request_utils.auth_client_ip(req), '203.0.113.9')

    def test_trusted_proxy_takes_rightmost_untrusted(self):
        """nginx 追加式 XFF：右端是代理看到的真实对端，左端是客户端可控区。"""
        req = self._request('127.0.0.1', '10.0.0.5, 203.0.113.9')
        self.assertEqual(request_utils.auth_client_ip(req), '203.0.113.9')

    def test_multi_layer_proxies_stripped(self):
        req = self._request('127.0.0.1', '203.0.113.9, 10.8.0.7')
        self.assertEqual(request_utils.auth_client_ip(req), '203.0.113.9')

    def test_all_hops_trusted_falls_back_to_remote(self):
        """剥完只剩代理自己：返回代理地址，业务白名单必然不匹配（fail-closed）。"""
        req = self._request('127.0.0.1', '10.8.0.7')
        self.assertEqual(request_utils.auth_client_ip(req), '127.0.0.1')

    def test_empty_xff_from_trusted_proxy(self):
        req = self._request('127.0.0.1', '')
        self.assertEqual(request_utils.auth_client_ip(req), '127.0.0.1')

    def test_display_ip_still_takes_first_hop(self):
        """审计展示口径不变：要的就是客户端声称的链路首段。"""
        req = self._request('127.0.0.1', '10.0.0.5, 203.0.113.9')
        self.assertEqual(request_utils.client_ip(req), '10.0.0.5')


class IpRuleTests(SimpleTestCase):
    def test_parse_mixed_separators(self):
        rules = request_utils.parse_ip_rules('10.0.0.1, 192.168.1.0/24\n2001:db8::/32')
        self.assertEqual(len(rules), 3)

    def test_single_ip_becomes_host_network(self):
        self.assertTrue(request_utils.ip_matches_rules('10.0.0.1', '10.0.0.1'))
        self.assertFalse(request_utils.ip_matches_rules('10.0.0.2', '10.0.0.1'))

    def test_cidr_match(self):
        self.assertTrue(request_utils.ip_matches_rules('192.168.1.200', '192.168.1.0/24'))
        self.assertFalse(request_utils.ip_matches_rules('192.168.2.1', '192.168.1.0/24'))

    def test_ipv6_match(self):
        self.assertTrue(request_utils.ip_matches_rules('2001:db8::1', '2001:db8::/32'))

    def test_invalid_entry_raises(self):
        with self.assertRaises(ValueError):
            request_utils.parse_ip_rules('10.0.0.1, not-an-ip')

    def test_invalid_text_is_not_a_match(self):
        """白名单文本坏了按不匹配处理，不能因为解析失败就放行。"""
        self.assertFalse(request_utils.ip_matches_rules('10.0.0.1', 'garbage'))

    def test_empty_rules_never_match(self):
        self.assertFalse(request_utils.ip_matches_rules('10.0.0.1', ''))


class EncryptedFieldTests(SimpleTestCase):
    """透明加密字段：存密文取明文，存量明文兼容，密文解不开要报错。"""

    def setUp(self):
        self.field = fields.EncryptedTextField()

    def test_round_trip(self):
        stored = self.field.get_prep_value('s3cr3t-password')
        self.assertTrue(fields.looks_encrypted(stored))
        self.assertNotIn('s3cr3t-password', stored)
        self.assertEqual(self.field.from_db_value(stored, None, None), 's3cr3t-password')

    def test_not_double_wrapped(self):
        """同一实例反复保存、迁移重跑都不该套第二层壳。"""
        once = self.field.get_prep_value('abc')
        self.assertEqual(self.field.get_prep_value(once), once)

    def test_legacy_plaintext_passes_through(self):
        """加密迁移尚未跑到的存量行原样返回，让迁移能分步跑、也允许回滚。"""
        self.assertEqual(self.field.from_db_value('legacy-plain', None, None), 'legacy-plain')

    def test_empty_preserved(self):
        """空值保留「未设置」语义：序列化器的 has_xxx 布尔依赖它。"""
        self.assertEqual(self.field.get_prep_value(''), '')
        self.assertEqual(self.field.from_db_value('', None, None), '')

    def test_broken_ciphertext_raises(self):
        """像密文却解不开 = key 变了。静默当明文用会得到极难排查的认证失败。"""
        with self.assertRaises(crypto.DecryptError):
            self.field.from_db_value(fields.FERNET_PREFIX + 'broken', None, None)


def make_record(msg='hello', level=logging.INFO, **extra):
    """造一条 LogRecord，等价于 logger.log(level, msg, extra=extra)。"""
    record = logging.LogRecord(
        name='apps.task_exec.runner', level=level, pathname='/app/apps/task_exec/runner.py',
        lineno=214, msg=msg, args=(), exc_info=None)
    record.__dict__.update(extra)
    return record


class JSONFormatterTests(SimpleTestCase):
    def setUp(self):
        self.formatter = log_utils.JSONFormatter(service='celery-exec')

    def emit(self, record):
        return json.loads(self.formatter.format(record))

    def test_base_fields(self):
        payload = self.emit(make_record('任务执行失败', level=logging.ERROR))
        self.assertEqual(payload['level'], 'ERROR')
        self.assertEqual(payload['service'], 'celery-exec')
        self.assertEqual(payload['logger'], 'apps.task_exec.runner')
        self.assertEqual(payload['module'], 'runner')
        self.assertEqual(payload['line'], 214)
        self.assertEqual(payload['msg'], '任务执行失败')

    def test_timestamp_is_rfc3339_utc_with_millis(self):
        """服务端按 _time 排序，格式错等于时间轴错乱，这里钉死形状。"""
        payload = self.emit(make_record())
        self.assertRegex(payload['ts'], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$')

    def test_single_line_even_with_traceback(self):
        """JSON 格式存在的首要理由：一条异常必须是一行，采集端无需猜多行边界。"""
        try:
            raise RuntimeError('boom')
        except RuntimeError:
            import sys
            record = make_record('炸了', level=logging.ERROR)
            record.exc_info = sys.exc_info()
        line = self.formatter.format(record)
        self.assertNotIn('\n', line)
        payload = json.loads(line)
        self.assertIn('RuntimeError: boom', payload['exc'])
        self.assertEqual(payload['msg'], '炸了')

    def test_extra_becomes_queryable_fields(self):
        payload = self.emit(make_record(task_id=12, exec_id=43))
        self.assertEqual(payload['task_id'], 12)
        self.assertEqual(payload['exec_id'], 43)

    def test_unserializable_extra_does_not_drop_the_log(self):
        """extra 里塞对象是常态；一个值不可序列化不该让整条日志消失。"""
        payload = self.emit(make_record(server=object()))
        self.assertIn('object object at', payload['server'])
        self.assertEqual(payload['msg'], 'hello')

    def test_message_args_interpolated(self):
        record = logging.LogRecord(
            name='apps', level=logging.INFO, pathname='/app/apps/x.py', lineno=1,
            msg='共 %s 台', args=(3,), exc_info=None)
        self.assertEqual(json.loads(self.formatter.format(record))['msg'], '共 3 台')

    def test_request_id_included_only_inside_request(self):
        self.assertNotIn('request_id', self.emit(make_record()))
        token = log_utils.set_request_id('abc123')
        try:
            self.assertEqual(self.emit(make_record())['request_id'], 'abc123')
        finally:
            log_utils.reset_request_id(token)


class RequestIdTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.seen = []
        self.middleware = log_utils.RequestIDMiddleware(self._view)

    def _view(self, request):
        self.seen.append(log_utils.current_request_id())
        return HttpResponse('ok')

    def test_generates_id_and_echoes_it(self):
        response = self.middleware(self.factory.get('/api/servers/'))
        self.assertEqual(len(self.seen), 1)
        self.assertTrue(self.seen[0])
        self.assertEqual(response['X-Request-ID'], self.seen[0])

    def test_reuses_upstream_id(self):
        """与 nginx access log 对齐的关键：上游给了就用上游的，不另生成。"""
        upstream = 'a' * 32
        response = self.middleware(
            self.factory.get('/api/servers/', HTTP_X_REQUEST_ID=upstream))
        self.assertEqual(self.seen[0], upstream)
        self.assertEqual(response['X-Request-ID'], upstream)

    def test_malformed_upstream_id_replaced(self):
        """直连 Django 时该头是客户端可控输入：超长/带控制字符的一律换掉。"""
        for bad in ('x' * 65, 'bad id', 'inject\nnewline', '../../etc/passwd', ''):
            with self.subTest(bad=bad):
                self.seen.clear()
                self.middleware(self.factory.get('/', HTTP_X_REQUEST_ID=bad))
                self.assertNotEqual(self.seen[0], bad)
                self.assertRegex(self.seen[0], log_utils.REQUEST_ID_RE)

    def test_context_reset_after_response(self):
        """gthread 下线程复用：不还原会让下一个请求继承上一个的 ID（串号）。"""
        self.middleware(self.factory.get('/'))
        self.assertEqual(log_utils.current_request_id(), '')

    def test_context_reset_even_when_view_raises(self):
        def boom(request):
            raise RuntimeError('boom')

        with self.assertRaises(RuntimeError):
            log_utils.RequestIDMiddleware(boom)(self.factory.get('/'))
        self.assertEqual(log_utils.current_request_id(), '')

    async def test_async_path(self):
        """daphne 的 ASGI HTTP 请求走这条：不支持异步会被 Django 包进线程池。"""
        seen = []

        async def view(request):
            seen.append(log_utils.current_request_id())
            return HttpResponse('ok')

        middleware = log_utils.RequestIDMiddleware(view)
        self.assertTrue(middleware.async_mode)
        response = await middleware(self.factory.get('/'))
        self.assertTrue(seen[0])
        self.assertEqual(response['X-Request-ID'], seen[0])
