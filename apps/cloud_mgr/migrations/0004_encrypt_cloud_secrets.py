"""把云账号 AccessKey Secret 与证书私钥的存量明文加密入库。

口径与 task_exec 的 0021 完全一致（原生 SQL + 显式加解密 + 幂等 + 可回滚），
理由见那个文件的说明。
"""
from django.db import migrations

from common.crypto import decrypt, encrypt
from common.fields import looks_encrypted

TARGETS = (
    ('cloud_account', ('secret_key',)),
    ('cloud_certificate', ('private_key_pem',)),
)


def _convert(schema_editor, transform):
    with schema_editor.connection.cursor() as cursor:
        for table_name, columns in TARGETS:
            table = schema_editor.quote_name(table_name)
            quoted = ', '.join(schema_editor.quote_name(c) for c in columns)
            cursor.execute(f'SELECT id, {quoted} FROM {table}')
            for row in cursor.fetchall():
                pk, values = row[0], row[1:]
                new = [transform(v) for v in values]
                if list(values) == new:
                    continue
                sets = ', '.join(f'{schema_editor.quote_name(c)} = %s' for c in columns)
                cursor.execute(f'UPDATE {table} SET {sets} WHERE id = %s', [*new, pk])


def forwards(apps, schema_editor):
    _convert(schema_editor, lambda v: v if not v or looks_encrypted(v) else encrypt(v))


def backwards(apps, schema_editor):
    _convert(schema_editor, lambda v: decrypt(v) if looks_encrypted(v) else v)


class Migration(migrations.Migration):
    dependencies = [('cloud_mgr', '0003_alter_certificate_private_key_pem_and_more')]
    operations = [migrations.RunPython(forwards, backwards)]
