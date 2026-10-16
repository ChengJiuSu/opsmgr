"""给存量角色补上新模块 `notify`（消息通知）的档位。

消息通知的配置接口原来挂在 `system` 模块的 write 档上，拆成独立模块后，缺失的键
按 fail-closed 视为 `none`——不补的话，原来能配通知的人（系统配置管理员）会突然
进不去。

补的口径与出厂矩阵一致，且**只补缺失的键**（重跑安全、不覆盖页面上配过的值）：

- super：`write`
- ops_admin：`write`（告警发到哪个群是日常运维的事，这是一次有意的放开——它原来
  没有 system 档，进不去通知配置）
- ops_member：`none`
- 自定义角色：跟随其 `system` 档位（write → write，否则 none）——保持「谁原来
  能配通知，现在仍然能配；谁原来不能，现在也不能」。

回滚删掉该键。
"""
from django.db import migrations

DEFAULTS = {
    'super': 'write',
    'ops_admin': 'write',
    'ops_member': 'none',
}


def forwards(apps, schema_editor):
    Role = apps.get_model('auth_mgr', 'Role')
    for role in Role.objects.all():
        permissions = role.permissions or {}
        if 'notify' in permissions:
            continue
        level = DEFAULTS.get(role.code) if role.is_builtin else None
        if level is None:
            level = 'write' if permissions.get('system') == 'write' else 'none'
        permissions['notify'] = level
        role.permissions = permissions
        role.save(update_fields=['permissions', 'updated_at'])


def backwards(apps, schema_editor):
    Role = apps.get_model('auth_mgr', 'Role')
    for role in Role.objects.all():
        permissions = role.permissions or {}
        if permissions.pop('notify', None) is None:
            continue
        role.permissions = permissions
        role.save(update_fields=['permissions', 'updated_at'])


class Migration(migrations.Migration):
    dependencies = [('auth_mgr', '0010_grant_topo_module')]
    operations = [migrations.RunPython(forwards, backwards)]
