"""给存量角色补上新模块 `topo`（拓扑画布）的档位。

新增模块的默认档是 `none`（Role.level_for 对缺失键 fail-closed），所以不补这一条，
库里已有的角色——包括内置的三个——打开画布菜单会是「谁都没权限」，而画布本身并不
比它引用的资产更敏感。

补的口径与出厂矩阵一致，且**只补缺失的键**：管理员若已在页面上给某角色配过
topo，这条迁移不覆盖（重跑安全）。

- super / ops_admin：`write`（日常运维的图就是他们在画）
- ops_member：`read_create`（与 cmdb / dict 同档：能看、能建自己的图，改删不给）
- 自定义角色：按其 cmdb 档位跟随——「能管资产到什么程度，就能画到什么程度」，
  比一律给 read 更贴合已有授权，也不会凭空放大：cmdb=none 的角色拿到的仍是 none。

回滚删掉该键，回到「模块不存在」的状态。
"""
from django.db import migrations

DEFAULTS = {
    'super': 'write',
    'ops_admin': 'write',
    'ops_member': 'read_create',
}
LEVELS = ('none', 'read', 'read_create', 'write')


def forwards(apps, schema_editor):
    Role = apps.get_model('auth_mgr', 'Role')
    for role in Role.objects.all():
        permissions = role.permissions or {}
        if 'topo' in permissions:
            continue
        level = DEFAULTS.get(role.code) if role.is_builtin else None
        if level is None:
            level = permissions.get('cmdb', 'none')
            if level not in LEVELS:
                level = 'none'
        permissions['topo'] = level
        role.permissions = permissions
        role.save(update_fields=['permissions', 'updated_at'])


def backwards(apps, schema_editor):
    Role = apps.get_model('auth_mgr', 'Role')
    for role in Role.objects.all():
        permissions = role.permissions or {}
        if permissions.pop('topo', None) is None:
            continue
        role.permissions = permissions
        role.save(update_fields=['permissions', 'updated_at'])


class Migration(migrations.Migration):
    dependencies = [('auth_mgr', '0009_split_role_modules')]
    operations = [migrations.RunPython(forwards, backwards)]
