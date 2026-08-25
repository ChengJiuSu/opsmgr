"""common 工具测试：TOTP（RFC 6238 官方向量）、Fernet 加解密、限速。"""
import base64
import time
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings

from . import crypto, ratelimit, totp

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
        with override_settings(MFA_SECRET_KEY='another-key'):
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
