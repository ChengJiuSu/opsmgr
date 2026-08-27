"""把连接凭据里的存量明文密钥加密入库（EncryptedTextField 上线配套）。

用原生 SQL 而不是 ORM：字段已是 EncryptedTextField，走 ORM 会叠上一层字段自身的
加解密，正反两个方向都难以推理。这里直接读写列值、在 Python 侧显式做加解密，
行为可预测。

**幂等**：只对「不像 Fernet 密文」的值加密，重跑不会二次套壳。
**可回滚**：reverse 把密文解回明文——否则回滚代码后库里留着解不开的密文，
凭据等于全部作废。
"""
from django.db import migrations

from common.crypto import decrypt, encrypt
from common.fields import looks_encrypted

TABLE = 'task_credential'
COLUMNS = ('password', 'private_key', 'become_password')


def _convert(schema_editor, transform):
    quoted = ', '.join(schema_editor.quote_name(c) for c in COLUMNS)
    table = schema_editor.quote_name(TABLE)
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(f'SELECT id, {quoted} FROM {table}')
        rows = cursor.fetchall()
        for row in rows:
            pk, values = row[0], row[1:]
            new = [transform(v) for v in values]
            if list(values) == new:
                continue
            sets = ', '.join(f'{schema_editor.quote_name(c)} = %s' for c in COLUMNS)
            cursor.execute(f'UPDATE {table} SET {sets} WHERE id = %s', [*new, pk])


def forwards(apps, schema_editor):
    _convert(schema_editor, lambda v: v if not v or looks_encrypted(v) else encrypt(v))


def backwards(apps, schema_editor):
    _convert(schema_editor, lambda v: decrypt(v) if looks_encrypted(v) else v)


class Migration(migrations.Migration):
    dependencies = [('task_exec', '0020_alter_credential_become_password_and_more')]
    operations = [migrations.RunPython(forwards, backwards)]
