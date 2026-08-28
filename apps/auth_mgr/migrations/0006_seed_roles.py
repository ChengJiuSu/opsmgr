"""播种内置角色，并把存量管理员挂到「超级管理」。

幂等（get_or_create + 只在缺字段时补），可回滚（reverse 解绑并删掉未被使用的内置角色）。

**行为中立**：`super` 的矩阵就是「全部模块 write」，等于原来的 `is_staff=True`；
非 staff 账号保持 role=None，回退到 roles.user_level 的 is_staff 分支（内置「普通
运维」矩阵），也与原来一致。所以这条迁移跑完谁的权限都没变——真正的收紧由管理员
在页面上逐个改角色完成，而不是一次迁移悄悄改掉所有人的权限。
"""
from django.db import migrations

#: 播种时那一版的内置角色，**在迁移里固化一份**而不是 import roles.BUILTIN_ROLES：
#: 迁移是历史，跟着线上代码一起变会让「新库 migrate 一遍」和「老库逐条 migrate」
#: 得到不同结果——模块矩阵后来拆成了 13 个（0009），这里必须仍是拆分前的 8 个，
#: 才能让 0009 的改写规则在新库上跑出与老库一致的矩阵。
BUILTIN_ROLES = (
    {
        'code': 'super', 'name': '超级管理',
        'remark': '全部模块读写，含用户/角色/凭据/API 凭证与生产执行',
        'can_execute': True, 'can_exec_prod': True,
        'permissions': {
            'cmdb': 'write', 'exec': 'write', 'credential': 'write', 'artifact': 'write',
            'cloud': 'write', 'agent': 'write', 'system': 'write', 'audit': 'write',
        },
    },
    {
        'code': 'ops_admin', 'name': '运维管理',
        'remark': '日常运维读写；连接凭据与 Agent 只读，系统管理不可见',
        'can_execute': True, 'can_exec_prod': True,
        'permissions': {
            'cmdb': 'write', 'exec': 'write', 'artifact': 'write', 'cloud': 'write',
            'credential': 'read', 'agent': 'read', 'system': 'none', 'audit': 'read',
        },
    },
    {
        'code': 'ops_member', 'name': '普通运维',
        'remark': '资产可查可增；任务可执行但不能改定义、不能碰生产',
        'can_execute': True, 'can_exec_prod': False,
        'permissions': {
            'cmdb': 'read_create', 'exec': 'read', 'artifact': 'read', 'cloud': 'read',
            'credential': 'none', 'agent': 'none', 'system': 'none', 'audit': 'read',
        },
    },
)


def forwards(apps, schema_editor):
    Role = apps.get_model('auth_mgr', 'Role')
    User = apps.get_model('auth_mgr', 'User')
    for spec in BUILTIN_ROLES:
        role, created = Role.objects.get_or_create(
            code=spec['code'],
            defaults={
                'name': spec['name'],
                'permissions': spec['permissions'],
                'can_execute': spec['can_execute'],
                'can_exec_prod': spec['can_exec_prod'],
                'is_builtin': True,
                'remark': spec['remark'],
            },
        )
        if not created and not role.is_builtin:
            # 存量环境里手工建过同 code 的角色：只补内置标记，不覆盖已调过的矩阵
            role.is_builtin = True
            role.save(update_fields=['is_builtin'])

    super_role = Role.objects.filter(code='super').first()
    if super_role:
        # 只补未绑角色的管理员；已绑角色的不动（可能已被人工改成别的角色）
        User.objects.filter(is_staff=True, role__isnull=True).update(role=super_role)


def backwards(apps, schema_editor):
    Role = apps.get_model('auth_mgr', 'Role')
    User = apps.get_model('auth_mgr', 'User')
    codes = [spec['code'] for spec in BUILTIN_ROLES]
    User.objects.filter(role__code__in=codes).update(role=None)
    Role.objects.filter(code__in=codes, users__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [('auth_mgr', '0005_role_user_allowed_environments_and_more')]
    operations = [migrations.RunPython(forwards, backwards)]
