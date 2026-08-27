"""角色权限矩阵：模块 × 权限档 + 若干独立能力位。

替代原来的二档 `is_staff`（管理员 / 非管理员）。二档撑不住真实诉求：想让某人
只管 CMDB 不能执行任务、只能对测试环境执行不能碰生产、外包只读——一个都做不到。

**刻意不做成腾讯云 CAM 那种策略语言**（action 注册表 + 资源 ARN + 通配符 + 求值
引擎 + 策略模拟器）。那套是给「一个平台服务成千上万互不信任的租户」设计的；这里
是 10 人内部平台、无多租户，上它等于用多租户的复杂度解决一个班的权限问题，还会
把「新增一个接口」从两行代码变成一次小设计。矩阵能表达的东西够用，而且矩阵本身
可读——不需要模拟器才能回答「这个角色能干什么」。

模块矩阵是策略文档的退化形式：将来真要上 CAM，可以机械翻译过去，不会白做。

## 权限档

| 档 | 含义 | 对应原来的权限类 |
|----|------|-----------------|
| `none` | 该模块完全不可见 | — |
| `read` | 只读 | IsAdminOrReadOnly 的读侧 |
| `read_create` | 可读、可新增，不可改删 | IsAdminOrReadCreate |
| `write` | 全量读写 | IsAdmin |

保留 `read_create` 而不是简化成三档：CMDB 现在就是「常规用户可查可增不可改删」
（见服务器与字典接口），砍掉这一档会造成行为回退。

## 独立能力位

按模块档位表达不了、又是高频诉求的，单独立字段，比塞进通用矩阵清楚得多：

- `can_execute`：可发起任务流执行；
- `can_exec_prod`：可对生产环境（Environment.is_prod）发起执行。

## 无角色的用户

严格回退到原来的 `is_staff` 语义，做到「谁的权限都没变」：

- staff → 全模块 write + 全能力位；
- 非 staff → 内置「普通运维」的**模块矩阵**（CMDB 可查可增、其余只读），
  但**不含执行能力**——角色落地前发起执行与处理暂停都是管理员专属。

所以存量账号不需要一次性配角色；要给执行能力就显式配一个角色。
"""
#: 权限档，由弱到强。索引即强弱序，用于「至少要 X 档」的比较
LEVELS = ('none', 'read', 'read_create', 'write')
LEVEL_LABELS = {
    'none': '无权限',
    'read': '只读',
    'read_create': '可读可新增',
    'write': '读写',
}

#: 模块注册表：key = 模块标识，value = 页面上展示的名称。
#: 粒度就是「一个业务 app / 一块菜单」——不是 150 个 action，够表达就行。
MODULES = {
    'cmdb': '资产与配置（服务器、字典、服务器分配）',
    'exec': '任务执行（任务流、变量、方法）',
    'credential': '连接凭据（SSH 密码与私钥）',
    'artifact': '构建制品',
    'cloud': '云控管理（云账号、域名、证书）',
    'agent': 'Agent 与执行通道',
    'system': '系统管理（用户、角色、系统配置、API 凭证）',
    'audit': '操作审计',
}

#: HTTP 方法 → 所需最低权限档
METHOD_LEVELS = {
    'GET': 'read', 'HEAD': 'read', 'OPTIONS': 'read',
    'POST': 'read_create',
    'PUT': 'write', 'PATCH': 'write', 'DELETE': 'write',
}

#: 内置角色（数据迁移据此播种，可在页面上改矩阵）。
#: 默认值刻意贴合当前线上行为：super 等于原来的 is_staff=True，
#: ops_member 等于原来的普通用户（CMDB 可查可增、其余只读），
#: 中间新增 ops_admin 一档——这正是二档模型缺的那一层。
BUILTIN_ROLES = (
    {
        'code': 'super', 'name': '超级管理', 'is_builtin': True,
        'remark': '全部模块读写，含用户/角色/凭据/API 凭证与生产执行',
        'can_execute': True, 'can_exec_prod': True,
        'permissions': {module: 'write' for module in MODULES},
    },
    {
        'code': 'ops_admin', 'name': '运维管理', 'is_builtin': True,
        'remark': '日常运维读写；连接凭据与 Agent 只读，系统管理不可见',
        'can_execute': True, 'can_exec_prod': True,
        'permissions': {
            'cmdb': 'write', 'exec': 'write', 'artifact': 'write', 'cloud': 'write',
            'credential': 'read', 'agent': 'read', 'system': 'none', 'audit': 'read',
        },
    },
    {
        'code': 'ops_member', 'name': '普通运维', 'is_builtin': True,
        'remark': '资产可查可增；任务可执行但不能改定义、不能碰生产',
        'can_execute': True, 'can_exec_prod': False,
        'permissions': {
            'cmdb': 'read_create', 'exec': 'read', 'artifact': 'read', 'cloud': 'read',
            'credential': 'none', 'agent': 'none', 'system': 'none', 'audit': 'read',
        },
    },
)


def level_index(level):
    """权限档强弱序；未知档位当 none（fail-closed）。"""
    try:
        return LEVELS.index(level)
    except ValueError:
        return 0


def satisfies(level, required):
    return level_index(level) >= level_index(required)


def required_level(method):
    """该 HTTP 方法所需的最低档；未知方法当 write（fail-closed）。"""
    return METHOD_LEVELS.get(str(method).upper(), 'write')


def user_level(user, module):
    """用户在该模块的权限档。

    无角色时回退 `is_staff` 语义：staff 全 write、其余按内置「普通运维」的矩阵。
    存量账号在角色落地前后行为不变，不必一次性给所有人配角色。
    """
    if not (user and getattr(user, 'is_authenticated', False)):
        return 'none'
    role = getattr(user, 'role', None)
    if role is not None and role.enabled:
        return role.level_for(module)
    if getattr(user, 'is_staff', False):
        return 'write'
    return _fallback_permissions().get(module, 'none')


def user_can(user, module, method='GET'):
    return satisfies(user_level(user, module), required_level(method))


def user_capability(user, name):
    """独立能力位（can_execute / can_exec_prod）。

    无角色时：staff 全给，**非 staff 一律不给**。

    这里刻意不套用内置「普通运维」的能力位（它是 can_execute=True）：角色落地前，
    发起执行与处理暂停都是管理员专属，非 staff 调 execute/confirm 会被拒。如果回退
    时把执行能力给出去，这条迁移就悄悄给所有存量普通账号开了执行权——回退的意义
    正是「谁的权限都没变」。要给执行能力，显式配一个角色。
    """
    if not (user and getattr(user, 'is_authenticated', False)):
        return False
    role = getattr(user, 'role', None)
    if role is not None and role.enabled:
        return bool(getattr(role, name, False))
    return bool(getattr(user, 'is_staff', False))


def permission_snapshot(user):
    """用户的完整权限快照，供 /api/auth/me/ 与 JWT 声明使用。

    前端据此决定按钮可见性——它只是「少让人点到必然 403 的按钮」，真正的判定始终
    在后端；前端拿到的快照过期最多让人多看到一个按钮，点下去照样被拒。
    """
    return {
        'role': getattr(getattr(user, 'role', None), 'code', '') or '',
        'role_name': getattr(getattr(user, 'role', None), 'name', '') or '',
        'modules': {module: user_level(user, module) for module in MODULES},
        'can_execute': user_capability(user, 'can_execute'),
        'can_exec_prod': user_capability(user, 'can_exec_prod'),
    }


def _fallback_permissions():
    return _builtin('ops_member')['permissions']


def _builtin(code):
    for item in BUILTIN_ROLES:
        if item['code'] == code:
            return item
    raise KeyError(code)  # 代码写错就炸，不静默返回空矩阵

