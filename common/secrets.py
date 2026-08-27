"""机读凭证的明文生成与哈希口径（全项目唯一实现）。

Agent 令牌、API 凭证共用这一份：口径散落两处，哪天有人改了其中一处的
摘要算法，另一处的存量凭证会静默失效，且要到线上调用 401 才发现。

只存哈希、不存明文：这类凭证不需要还原（每次校验都是「拿明文算摘要比对」），
库泄露时攻击者拿到的哈希无法直接当 key 用。需要可还原的秘密（Webhook 签名
密钥要用原值重算 HMAC）走 common.crypto 的对称加密，不走这里。
"""
import hashlib
import secrets


def generate_secret(nbytes=32):
    """生成 URL 安全的随机明文（约 43 字符 / 256 bit 熵）。"""
    return secrets.token_urlsafe(nbytes)


def hash_secret(raw):
    """明文 → sha256 hex。

    这里刻意不用 PBKDF2/bcrypt：凭证是 256 bit 随机串而非人选的密码，不存在
    字典攻击面，而机读接口每个请求都要校验一次，慢哈希会把它变成瓶颈。
    """
    return hashlib.sha256(str(raw).encode('utf-8')).hexdigest()
