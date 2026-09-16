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
import re
import time
from datetime import datetime

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from . import dedupe

logger = logging.getLogger(__name__)

#: 时间戳允许的偏差（秒），双向
TIMESTAMP_WINDOW = 300
#: nonce 去重的命名空间与保留时长（取窗口两倍，覆盖时钟漂移两端）
NONCE_NAMESPACE = 'nonce'
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


#: 卡片回调的时间戳不是 Unix 秒，而是 Go 的 time.Time 默认格式：
#:   2026-09-16 17:22:51.152344108 +0800 CST m=+526362.485448901
#: 事件订阅那条给的又是秒。两条路不一致，只认数字会把卡片按钮全拒掉。
#: 只取日期时间与 ±HHMM 偏移，纳秒、时区缩写（CST 这种缩写有歧义）与 Go 的
#: 单调时钟尾巴（m=+…）一律忽略。
GO_TIME_RE = re.compile(
    r'^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})(?:\.\d+)?\s*([+-]\d{4})')
#: 毫秒时间戳的下界（2001-09-09 之后的秒级时间戳都小于它）
MILLIS_THRESHOLD = 10 ** 11


def parse_timestamp(timestamp):
    """把飞书给的时间戳解析成 Unix 秒。解析不了抛 CallbackError。"""
    raw = str(timestamp or '').strip()
    if not raw:
        raise CallbackError('缺少时间戳')
    if raw.lstrip('-').isdigit():
        value = int(raw)
        # 有的接口给毫秒；按量级判，别让 1970 年的偏差把请求拒掉
        return value // 1000 if value >= MILLIS_THRESHOLD else value
    matched = GO_TIME_RE.match(raw)
    if not matched:
        raise CallbackError(f'时间戳非法：{raw!r}')
    text, offset = matched.groups()
    try:
        parsed = datetime.strptime(f'{text.replace("T", " ")} {offset}',
                                   '%Y-%m-%d %H:%M:%S %z')
    except ValueError as exc:
        raise CallbackError(f'时间戳非法：{raw!r}') from exc
    return int(parsed.timestamp())


def verify_timestamp(timestamp, now=None):
    """时间戳偏差校验（秒级）。"""
    sent = parse_timestamp(timestamp)
    drift = abs((now if now is not None else int(time.time())) - sent)
    if drift > TIMESTAMP_WINDOW:
        raise CallbackError(f'时间戳偏差 {drift}s 超过 {TIMESTAMP_WINDOW}s，拒绝处理')


def consume_nonce(nonce):
    """消费一次 nonce；窗口内重复出现即判为重放（判重实现见 dedupe.py）。"""
    if not nonce:
        raise CallbackError('缺少 nonce')
    try:
        fresh = dedupe.consume(NONCE_NAMESPACE, str(nonce), NONCE_TTL)
    except dedupe.DedupeUnavailable as exc:
        # fail-closed：判不了重就当重放。放行的后果是可被重放触发生产执行
        raise CallbackError('去重缓存不可用，拒绝处理') from exc
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


#: 事件订阅地址验证：飞书**不给这一次请求带签名头**（那一步它只想确认你能解密并
#: 回显 challenge），所以这条路径单独放行签名/时间戳/nonce 三道
TYPE_URL_VERIFICATION = 'url_verification'


def parse_body(app, body):
    """请求体 → (明文 payload, 是否加密过)。加密的解密，明文的原样返回。

    返回「是否加密」而不是在这里拦：正式事件必须加密、地址验证可以明文，
    这个判断属于调用方的策略，解析函数只负责把体变成 dict。
    """
    try:
        raw = json.loads((body or b'').decode('utf-8'))
    except (ValueError, UnicodeDecodeError) as exc:
        raise CallbackError(f'请求体不是合法 JSON：{exc}') from exc
    if not isinstance(raw, dict):
        raise CallbackError('请求体不是 JSON 对象')
    if 'encrypt' not in raw:
        return raw, False

    plain = decrypt(app.encrypt_key, raw.get('encrypt'))
    try:
        payload = json.loads(plain)
    except ValueError as exc:
        raise CallbackError(f'解密结果不是合法 JSON：{exc}') from exc
    if not isinstance(payload, dict):
        raise CallbackError('解密结果不是 JSON 对象')
    return payload, True


def verify_callback(app, timestamp, nonce, signature, body):
    """完整校验一次回调，返回明文 payload（dict）。

    调用方（视图）只需先判来源 IP 与 `app.is_ready()`，其余全在这里。

    **带签名头**（正式事件推送）走完整五道，顺序是「签名 → 时间戳 → nonce →
    解密 → token」：前三道都不需要解密，便宜且能挡掉绝大多数噪声；nonce 放在
    解密前，避免用重放请求做解密运算。

    **不带签名头**只放行一种情况：事件订阅的地址验证（`url_verification`）。
    飞书在那一步不发签名头——它只想确认你能解密并回显 challenge。这条路径仍要
    通过解密与 Verification Token 两道（伪造者没有 Encrypt Key 就构造不出可解密
    的体），而且它**不触发任何业务动作**，只回显一个 challenge，放行的面是封闭的。
    除此之外，缺签名头一律拒——不能让「不带签名」变成绕开验签的通道。
    """
    if signature:
        verify_signature(app.encrypt_key, timestamp, nonce, body, signature)
        verify_timestamp(timestamp)
        consume_nonce(nonce)
        payload, encrypted = parse_body(app, body)
        if not encrypted:
            # 带签名却没加密：飞书不会这么发，当异常来源处理
            raise CallbackError('请求体未加密（飞书事件订阅未开启加密？）')
        verify_token(app.verification_token, payload)
        return payload

    payload, _encrypted = parse_body(app, body)
    if payload.get('type') != TYPE_URL_VERIFICATION:
        raise CallbackError('缺少签名头 X-Lark-Signature')
    verify_token(app.verification_token, payload)
    return payload
