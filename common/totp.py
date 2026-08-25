"""TOTP（RFC 6238 / HOTP RFC 4226）：标准库实现，无第三方依赖。

只有 HMAC + 大端计数器 + 动态截断三步，算法本身比引一个包更小。正确性由
`common/tests.py` 里 RFC 6238 附录 B 的官方测试向量守着。

与 Google Authenticator / Microsoft Authenticator / 1Password 等通用 App
的默认参数一致：SHA1、6 位、30 秒。**别改这三个默认值**——App 端扫码只读
otpauth URI 里的 secret 和 issuer，多数不认 algorithm/digits 参数，改了就
是双方各算一套码。
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote, urlencode

DIGITS = 6
PERIOD = 30           # 秒
SECRET_BYTES = 20     # 160 bit，RFC 4226 §4 R6 推荐值（base32 后 32 字符）
# 校验容差：前后各一个时间窗（±30s），覆盖用户手输延迟与轻微时钟漂移。
# 再放宽就等于把有效码数量成倍放大，不划算。
VALID_WINDOW = 1


def random_secret():
    """生成新的 base32 密钥（无填充，便于手输与二维码）。"""
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode('ascii').rstrip('=')


def normalize_secret(secret):
    """去空格、补 base32 填充、转大写——手输密钥常带空格与小写。"""
    cleaned = str(secret).replace(' ', '').replace('-', '').upper()
    padding = '=' * (-len(cleaned) % 8)
    return cleaned + padding


def code_at(secret, counter, digits=DIGITS):
    """给定计数器（= 时间戳 // period）算出验证码。"""
    key = base64.b32decode(normalize_secret(secret), casefold=True)
    digest = hmac.new(key, struct.pack('>Q', counter), hashlib.sha1).digest()
    # 动态截断（RFC 4226 §5.4）：末字节低 4 位作偏移，取 4 字节去掉最高位
    offset = digest[-1] & 0x0F
    truncated = struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(truncated % (10 ** digits)).zfill(digits)


def current_counter(at=None):
    return int((at if at is not None else time.time()) // PERIOD)


def verify(secret, code, at=None, valid_window=VALID_WINDOW, after_counter=None):
    """校验验证码，命中返回该码所属的 counter，否则返回 None。

    after_counter：只接受**严格大于**它的窗口，用于防重放——同一个 30 秒窗口
    的码用过一次就不再认第二次（调用方把返回值存起来下次传进来）。
    """
    code = str(code or '').strip().replace(' ', '')
    if not code.isdigit() or len(code) != DIGITS:
        return None
    base = current_counter(at)
    for offset in range(-valid_window, valid_window + 1):
        counter = base + offset
        if counter < 0:
            continue
        if after_counter is not None and counter <= after_counter:
            continue
        # 定时安全比较：避免按前缀匹配长度泄露信息
        if hmac.compare_digest(code_at(secret, counter), code):
            return counter
    return None


def provisioning_uri(secret, account, issuer):
    """otpauth:// URI，供前端渲染二维码（后端不画图，不引 Pillow）。

    label 形如 `issuer:account`，issuer 同时作为参数重复给出——两者都给才能
    让各家 App 都正确显示归属。
    """
    label = quote(f'{issuer}:{account}', safe='')
    query = urlencode({
        'secret': str(secret).replace('=', ''),
        'issuer': issuer,
        'algorithm': 'SHA1',
        'digits': DIGITS,
        'period': PERIOD,
    })
    return f'otpauth://totp/{label}?{query}'
