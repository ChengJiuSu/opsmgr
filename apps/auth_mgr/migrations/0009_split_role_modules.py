"""把 8 个粗模块的权限矩阵改写成 13 个细模块。

拆分理由见 apps/auth_mgr/roles.py 的模块文档字符串（四处「给 A 就顺带给了 B」）。
这条迁移只改 Role.permissions 这一个 JSON 列的内容，不动表结构。

## 改写规则

1. **拆出来的子模块继承父模块的档位**（cmdb → cmdb+dict、exec → exec+method+
   variable、cloud → cloud+cloud_account、system → account+system），再按新模块
   的可选档位**向下取整**——取整只会更小，不会凭空放大任何人的权限。
2. **内置角色若还是出厂矩阵，直接换成新的出厂矩阵**（见 NEW_DEFAULTS）。判据是
   「与旧出厂矩阵逐键相等」：管理员改过的内置角色走规则 1，不覆盖人工调整。
3. 自定义角色一律走规则 1。

## 哪些人的权限真的变小了

- 旧 `cloud=write` 同时给了域名解析和云账号 AK/SK。规则 1 会把 `cloud_account`
  也置为 write（不改变现状），但**未被改过的内置「运维管理」走规则 2**，云账号
  收成 read（够在域名/证书页选账号，AccessKey 打码、改不了）——这正是拆分要修的
  那条：日常改域名的人不该顺手拿到整个云资产。
- 旧 `exec=read_create` 能新增方法（MethodViewSet 当时按 HTTP 方法推档，POST 只
  要 read_create）。新 `method` 模块没有 read_create 档，向下取整成 read，即
  「能看方法、不能新增」。这是有意收紧：新增一条方法 = 把任意命令带进平台。
- 旧 `system=read` / `agent=read_create` / `audit=write` 之类，对应接口当时本就
  不放行（那些 ViewSet 要求 write），取整后行为不变。

回滚（backwards）按「子模块取最强档」合回 8 模块，与 forwards 不是严格互逆：
规则 2 覆盖掉的内置矩阵回滚后是新出厂矩阵合并的结果，不是历史上那份。回滚只是
为了让 migrate 能倒着走，不承诺还原人工调整。
"""
from django.db import migrations

#: 旧 → 新：一个旧模块拆成哪几个新模块（未列出的模块名字与语义都没变）
SPLIT = {
    'cmdb': ('cmdb', 'dict'),
    'exec': ('exec', 'method', 'variable'),
    'cloud': ('cloud', 'cloud_account'),
    'system': ('account', 'system'),
}
UNCHANGED = ('credential', 'artifact', 'agent', 'audit')

LEVELS = ('none', 'read', 'read_create', 'write')

#: 新模块的可选档位（与 roles.MODULE_LEVELS 一致；迁移里固化一份，
#: 免得将来改了常量再跑历史迁移会得到另一种结果）
NEW_MODULE_LEVELS = {
    'audit': ('none', 'read'),
    'artifact': ('none', 'read', 'write'),
    'method': ('none', 'read', 'write'),
    'cloud_account': ('none', 'read', 'write'),
    'account': ('none', 'write'),
    'system': ('none', 'write'),
    'agent': ('none', 'read', 'write'),
}

#: 拆分前的出厂矩阵（用于判断内置角色有没有被人改过）
OLD_DEFAULTS = {
    'super': {m: 'write' for m in
              ('cmdb', 'exec', 'credential', 'artifact', 'cloud', 'agent', 'system', 'audit')},
    'ops_admin': {
        'cmdb': 'write', 'exec': 'write', 'artifact': 'write', 'cloud': 'write',
        'credential': 'read', 'agent': 'read', 'system': 'none', 'audit': 'read',
    },
    'ops_member': {
        'cmdb': 'read_create', 'exec': 'read', 'artifact': 'read', 'cloud': 'read',
        'credential': 'none', 'agent': 'none', 'system': 'none', 'audit': 'read',
    },
}

#: 拆分后的出厂矩阵
NEW_DEFAULTS = {
    'super': {
        'cmdb': 'write', 'dict': 'write', 'exec': 'write', 'method': 'write',
        'variable': 'write', 'artifact': 'write', 'credential': 'write',
        'cloud': 'write', 'cloud_account': 'write', 'agent': 'write',
        'account': 'write', 'system': 'write', 'audit': 'read',
    },
    'ops_admin': {
        'cmdb': 'write', 'dict': 'write', 'exec': 'write', 'method': 'write',
        'variable': 'write', 'artifact': 'write', 'cloud': 'write',
        'credential': 'read', 'agent': 'read', 'audit': 'read',
        'cloud_account': 'read', 'account': 'none', 'system': 'none',
    },
    'ops_member': {
        'cmdb': 'read_create', 'dict': 'read_create', 'exec': 'read', 'method': 'read',
        'variable': 'read', 'artifact': 'read', 'cloud': 'read', 'audit': 'read',
        'credential': 'none', 'cloud_account': 'none',
        'agent': 'none', 'account': 'none', 'system': 'none',
    },
}
NEW_REMARKS = {
    'super': '全部模块读写，含用户/角色/凭据/云账号/API 凭证与生产执行',
    'ops_admin': '日常运维读写；云账号/连接凭据/Agent 只读，用户、角色与系统配置不可见',
    'ops_member': '资产与字典可查可增；任务可执行但不能改定义、不能改方法、不能碰生产',
}


def _clamp(level, module):
    """把档位向下取整到该模块的可选档：取整只会更小，不会放大权限。"""
    allowed = NEW_MODULE_LEVELS.get(module, LEVELS)
    for candidate in reversed(allowed):
        if LEVELS.index(candidate) <= LEVELS.index(level):
            return candidate
    return 'none'


def _to_new(old):
    new = {}
    for old_module, targets in SPLIT.items():
        level = old.get(old_module, 'none')
        for module in targets:
            new[module] = _clamp(level if level in LEVELS else 'none', module)
    for module in UNCHANGED:
        level = old.get(module, 'none')
        new[module] = _clamp(level if level in LEVELS else 'none', module)
    return new


def _to_old(new):
    old = {}
    for old_module, targets in SPLIT.items():
        levels = [new.get(m, 'none') for m in targets]
        old[old_module] = max(levels, key=lambda lv: LEVELS.index(lv) if lv in LEVELS else 0)
    for module in UNCHANGED:
        old[module] = new.get(module, 'none')
    return old


#: 只有新矩阵才有的模块标识，用来识别「这条角色已经是新矩阵了」
NEW_ONLY = ('dict', 'method', 'variable', 'cloud_account', 'account')


def forwards(apps, schema_editor):
    Role = apps.get_model('auth_mgr', 'Role')
    for role in Role.objects.all():
        permissions = role.permissions or {}
        if any(module in permissions for module in NEW_ONLY):
            continue  # 已经是新矩阵（重跑迁移 / 页面上新建的角色），不再改写
        if role.is_builtin and permissions == OLD_DEFAULTS.get(role.code):
            role.permissions = dict(NEW_DEFAULTS[role.code])
            role.remark = NEW_REMARKS[role.code]
            role.save(update_fields=['permissions', 'remark', 'updated_at'])
            continue
        role.permissions = _to_new(permissions)
        role.save(update_fields=['permissions', 'updated_at'])


def backwards(apps, schema_editor):
    Role = apps.get_model('auth_mgr', 'Role')
    for role in Role.objects.all():
        role.permissions = _to_old(role.permissions or {})
        role.save(update_fields=['permissions', 'updated_at'])


class Migration(migrations.Migration):
    dependencies = [('auth_mgr', '0008_mark_service_users')]
    operations = [migrations.RunPython(forwards, backwards)]
