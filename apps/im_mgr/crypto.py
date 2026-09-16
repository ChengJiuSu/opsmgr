"""飞书回调的解密与验签——两条公网入口的第一道门。

飞书事件订阅与卡片回调打到 `/api/feishu/<slug>/event|card/`。路径带随机串只是
让扫描器打不到入口，**真正的门是这里**：任何人都可以直接 POST 这两个地址，
不验签就等于把「触发任务执行」开放给全互联网。

四道，顺序固定、任一不过即拒：

1. **来源 IP** —— 在视图层用 `FeishuApp.ip_allowed(auth_client_ip(request))` 判
   （不在本模块：取 IP 要拿 request）；
2. **签名** —— `sha256(timestamp + nonce + encrypt_key + 原始请求体)`，与
   `X-Lark-Signature` 恒定时间比对。注意签的是**原始字节**，不是解析后再
   序列化的 JSON——重新序列化会改键序与空格，签名必不匹配；
3. **时间戳** —— 偏差超过 ±5 分钟拒，压缩重放窗口；
4. **nonce 去重** —— 窗口内同一 nonce 只接受一次，把重放窗口压到 0。

解密用 AES-256-CBC，key = `sha256(encrypt_key)`，IV 取密文前 16 字节
（飞书开放平台的固定算法）。

**这里一律 fail-closed**：缓存不可用时 nonce 判重失败，按「已重放」拒绝。
与 common/sudo.py 同一取舍——放行的后果是可被重放触发生产执行，拒绝的后果
只是飞书那边报个失败。
"""
import base64
import hashlib
import hmac
import json
import logging
import time

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from django.core.cache import cache

logger = logging.getLogger(__name__)

#: 时间戳允许的偏差（秒），双向
TIMESTAMP_WINDOW = 300
#: nonce 去重的缓存前缀与保留时长（取窗口两倍，覆盖时钟漂移两端）
NONCE_PREFIX = 'feishu:nonce:'
NONCE_TTL = TIMESTAMP_WINDOW * 2

AES_BLOCK_SIZE = 16


class CallbackError(Exception):
    """回调校验失败。消息只进日志，不回给调用方——回包必须恒定。"""


def decrypt(encrypt_key, encrypt_value):
    """解密飞书回调体的 `encrypt` 字段，返回明文字符串。"""
    if not encrypt_key:
        raise CallbackError('未配置 Encrypt Key，无法解密回调')
    if not encrypt_value:
        raise CallbackError('回调体缺少 encrypt 字段（飞书事件订阅未开启加密？）')
    try:
        data = base64.b64decode(encrypt_value)
    except (ValueError, TypeError) as exc:
        raise CallbackError(f'encrypt 字段不是合法 base64：{exc}') from exc
    if len(data) <= AES_BLOCK_SIZE or (len(data) - AES_BLOCK_SIZE) % AES_BLOCK_SIZE:
        raise CallbackError('密文长度非法')

    key = hashlib.sha256(str(encrypt_key).encode('utf-8')).digest()
    iv, ciphertext = data[:AES_BLOCK_SIZE], data[AES_BLOCK_SIZE:]
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    return _unpad(padded).decode('utf-8', errors='strict')


def _unpad(padded):
    """去 PKCS#7 填充。

    填充长度非法通常意味着 Encrypt Key 配错（解出来是随机字节），报错比返回
    半截明文好——后者会变成一个「JSON 解析失败」的假象，指向完全错误的方向。
    """
    if not padded:
        raise CallbackError('解密结果为空（Encrypt Key 可能配置错误）')
    size = padded[-1]
    if not 1 <= size <= AES_BLOCK_SIZE or len(padded) < size:
        raise CallbackError('解密结果填充非法（Encrypt Key 可能配置错误）')
    return padded[:-size]


def verify_signature(encrypt_key, timestamp, nonce, body, signature):
    """校验 X-Lark-Signature。body 必须是**原始请求体字节**。"""
    if not signature:
        raise CallbackError('缺少签名头 X-Lark-Signature')
    if not encrypt_key:
        raise CallbackError('未配置 Encrypt Key，无法验签')
    prefix = f'{timestamp}{nonce}{encrypt_key}'.encode('utf-8')
    expected = hashlib.sha256(prefix + (body or b'')).hexdigest()
    if not hmac.compare_digest(expected, str(signature).strip()):
        raise CallbackError('签名校验失败')


def verify_timestamp(timestamp, now=None):
    """时间戳偏差校验（秒级）。"""
    try:
        sent = int(str(timestamp).strip())
    except (TypeError, ValueError) as exc:
        raise CallbackError(f'时间戳非法：{timestamp!r}') from exc
    drift = abs((now if now is not None else int(time.time())) - sent)
    if drift > TIMESTAMP_WINDOW:
        raise CallbackError(f'时间戳偏差 {drift}s 超过 {TIMESTAMP_WINDOW}s，拒绝处理')


def consume_nonce(nonce):
    """消费一次 nonce；窗口内重复出现即判为重放。

    用 `cache.add`（原子的 set-if-absent），不是 get + set——后者在并发重放下
    两个请求会同时读到「不存在」然后都放行。
    """
    if not nonce:
        raise CallbackError('缺少 nonce')
    try:
        fresh = cache.add(NONCE_PREFIX + str(nonce), 1, timeout=NONCE_TTL)
    except Exception:  # noqa: BLE001 - 缓存后端异常类型由实现决定
        # fail-closed：判不了重就当重放。放行的后果是可被重放触发生产执行
        logger.warning('nonce 去重缓存不可用，按重放处理', exc_info=True)
        raise CallbackError('去重缓存不可用，拒绝处理')
    if not fresh:
        raise CallbackError('nonce 重复，疑似重放请求')


def payload_token(payload):
    """取回调体里的 Verification Token。

    v2 事件在 `header.token`，url_verification 与旧版卡片回调在顶层 `token`。
    两种都要认：事件订阅地址验证那一次走的就是前者之外的形态。
    """
    if not isinstance(payload, dict):
        return ''
    header = payload.get('header')
    if isinstance(header, dict) and header.get('token'):
        return str(header['token'])
    return str(payload.get('token') or '')


def verify_token(expected, payload):
    if not expected:
        raise CallbackError('未配置 Verification Token，无法校验回调来源')
    if not hmac.compare_digest(str(expected), payload_token(payload)):
        raise CallbackError('Verification Token 不匹配')


def verify_callback(app, timestamp, nonce, signature, body):
    """完整校验一次回调，返回解密后的明文 payload（dict）。

    调用方（视图）只需先判来源 IP 与 `app.is_ready()`，其余全在这里。
    顺序刻意是「签名 → 时间戳 → nonce → 解密 → token」：前三道都不需要解密，
    便宜且能挡掉绝大多数噪声；nonce 放在解密前，避免用重放请求做解密运算。
    """
    verify_signature(app.encrypt_key, timestamp, nonce, body, signature)
    verify_timestamp(timestamp)
    consume_nonce(nonce)

    try:
        raw = json.loads((body or b'').decode('utf-8'))
    except (ValueError, UnicodeDecodeError) as exc:
        raise CallbackError(f'请求体不是合法 JSON：{exc}') from exc
    if not isinstance(raw, dict):
        raise CallbackError('请求体不是 JSON 对象')

    plain = decrypt(app.encrypt_key, raw.get('encrypt'))
    try:
        payload = json.loads(plain)
    except ValueError as exc:
        raise CallbackError(f'解密结果不是合法 JSON：{exc}') from exc
    if not isinstance(payload, dict):
        raise CallbackError('解密结果不是 JSON 对象')

    verify_token(app.verification_token, payload)
    return payload
