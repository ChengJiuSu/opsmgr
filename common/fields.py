"""透明加密的模型字段：库里存密文，Python 侧照常当字符串用。

为什么用自定义字段而不是 `get_xxx()/set_xxx()` 方法：这几个秘密
（连接凭据的密码/私钥/提权密码、云账号 AccessKey Secret、证书私钥）在
runner、providers、deploy、序列化器里有几十处读取点，改成方法调用等于把
每一处都摸一遍，漏一处的表现是「密码变成 gAAAAA... 然后 SSH 认证失败」。
字段层做掉，调用方零改动。

代价与边界：
- **不能对这些字段做 filter/order_by/icontains**：库里是密文，条件匹配不到。
  这几个字段本来也没有按值查询的语义（谁会按密码搜凭据）。
- 明文兼容：加密前入库的存量行是明文，`from_db_value` 认不出 Fernet 特征时
  原样返回，让加密迁移可以分步跑、也允许回滚。但**看起来像密文却解不开**时
  抛错——那是 key 变了，静默把密文当密码用会得到一个极难排查的认证失败。
"""
import logging

from django.db import models

from common.crypto import DecryptError, decrypt, encrypt

logger = logging.getLogger(__name__)

#: Fernet token 的固定前缀（version 0x80 + 时间戳 base64 后的头部）。
#: 用它区分「存量明文」与「密文」，避免把明文当解密失败、把密文当明文用。
FERNET_PREFIX = 'gAAAAA'


def looks_encrypted(value):
    return isinstance(value, str) and value.startswith(FERNET_PREFIX)


class EncryptedTextField(models.TextField):
    """存密文、取明文的文本字段。

    空值原样存取（保留「未设置」语义，序列化器的 has_xxx 布尔依赖它）。
    一律用 TEXT 而不是 VARCHAR：密文比明文长得多（32 字符的密码加密后约 130
    字符），沿用原来的 max_length 会在保存时被数据库截断成解不开的半截密文。
    """

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if not value:
            return value
        # 已是密文（同一实例反复保存、或迁移重跑）不再套一层
        return value if looks_encrypted(value) else encrypt(value)

    def from_db_value(self, value, expression, connection):
        if not value:
            return value
        if not looks_encrypted(value):
            return value  # 存量明文：加密迁移尚未跑到，原样返回
        try:
            return decrypt(value)
        except DecryptError:
            logger.error('字段密文无法解开（SECRET_ENC_KEY/SECRET_KEY 变更？）：%s.%s',
                         getattr(self, 'model', None), self.name)
            raise
