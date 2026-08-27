"""对称加密（Fernet）：给库里必须**可还原**的秘密用。

使用者（都是「每次用都要拿原值」、只存哈希做不到的场景）：

- MFA 的 TOTP 密钥——每次校验都要用原值重算验证码；
- 执行变量里标记为敏感的值（`ExecVariable.is_secret`）——要原样注入 ansible；
- 主机连接凭据的密码/私钥/提权密码——要下发给 ansible 建连；
- 云账号 AccessKey Secret、Webhook 签名密钥——要用原值调厂商 API / 重算 HMAC。

只存哈希的凭证（Agent 令牌、API 凭证的 secret）走 common/secrets.py，不走这里。

密钥来源按序回退：`SECRET_ENC_KEY` → `MFA_SECRET_KEY` → `SECRET_KEY`。
`MFA_SECRET_KEY` 是历史名字（那时唯一使用者是 MFA），保留以免存量部署换名后
密文全部解不开；新部署配 `SECRET_ENC_KEY`。

**换 key 会让已入库的密文全部解不开**：表现为 MFA 校验一律失败、任务执行报
变量解密失败、ansible 建连失败。所以生产必须显式配置并与 `SECRET_KEY` 分开
轮换，换 key 前先导出重新加密。

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
    material = (getattr(settings, 'SECRET_ENC_KEY', '')
                or getattr(settings, 'MFA_SECRET_KEY', '')
                or settings.SECRET_KEY)
    if not material:
        raise ImproperlyConfigured(
            'SECRET_ENC_KEY / MFA_SECRET_KEY / SECRET_KEY 均为空，无法加密存储秘密')
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
