"""对称加密（Fernet）：给库里必须可还原的秘密用。

当前唯一使用者是 MFA 的 TOTP 密钥。密钥不能像密码那样只存哈希（每次校验都
要用原值重算验证码），但明文入库等于「库泄露即可自造任何人的验证码」，MFA
就白做了——所以单独加一层。

密钥来源 `settings.MFA_SECRET_KEY`，留空时从 `SECRET_KEY` 派生。**换 key 会
让已入库的密文全部解不开**（表现为老用户 MFA 校验一律失败，需要管理员重置
绑定），所以生产建议显式配 `MFA_SECRET_KEY` 并与 `SECRET_KEY` 分开轮换。

cryptography 已随 ansible-core 进环境，requirements 里显式声明以免哪天
ansible 换了依赖链就静默丢失。
"""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class DecryptError(Exception):
    """密文无法解开（key 变了 / 数据被改坏）。"""


def _fernet():
    material = getattr(settings, 'MFA_SECRET_KEY', '') or settings.SECRET_KEY
    if not material:
        raise ImproperlyConfigured('MFA_SECRET_KEY 与 SECRET_KEY 均为空，无法加密存储密钥')
    # Fernet 要 32 字节 urlsafe-base64 key；用 sha256 把任意长度配置值归一
    key = base64.urlsafe_b64encode(hashlib.sha256(material.encode('utf-8')).digest())
    return Fernet(key)


def encrypt(text):
    """明文 → 密文字符串（空值原样返回，便于「未设置」语义）。"""
    if not text:
        return ''
    return _fernet().encrypt(str(text).encode('utf-8')).decode('ascii')


def decrypt(token):
    """密文字符串 → 明文；解不开抛 DecryptError（调用方给出可行动的提示）。"""
    if not token:
        return ''
    try:
        return _fernet().decrypt(str(token).encode('ascii')).decode('utf-8')
    except (InvalidToken, ValueError, TypeError) as exc:
        raise DecryptError('密文无法解开：MFA_SECRET_KEY/SECRET_KEY 可能已变更') from exc
