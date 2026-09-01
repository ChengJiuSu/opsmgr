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

## 模块粒度：一个模块 = 一块菜单 + 一个风险面

粒度按「一块菜单」切，但**风险面不同就必须拆开**——否则矩阵表面上有四档，实际
授权时只能按里面最危险的那一项来给，中间档形同虚设。第一版把 13 个页面压成 8 个
模块，压出了四处「给 A 就顺带给了 B」，都是提权面：

| 原模块 | 混在一起的东西 | 拆成 |
|--------|----------------|------|
| `cloud` | 域名解析 / 证书（日常运维）+ 云账号 AK/SK（拿到即整个云资产） | `cloud` + `cloud_account` |
| `system` | 系统配置 + 用户 / 角色 / API 凭证（改角色 = 给自己加权） | `system` + `account` |
| `exec` | 任务流 + 方法库（模板里能写任意命令）+ 变量（含密码类真值） | `exec` + `method` + `variable` |
| `cmdb` | 服务器台账 + 五张基础字典（改环境 is_prod 可绕过 can_exec_prod；项目组 / 环境同时是数据范围的维度） | `cmdb` + `dict` |

拆完 13 个模块，正好一个模块对一块菜单/一个 ViewSet 群，**没有一个模块的写权限
能顺带拿到另一个模块的能力**。这是拆分的判据，不是「越细越好」——细到 action
级就回到 CAM 那条路上去了。

## 每个模块的可选档位

不是每个模块四档都有意义：审计没有写接口、制品只能由 CI 上报（页面上没有新增）、
方法「只能新增不能改」并不比「能改」安全（新增一条方法同样是把任意命令带进平台）。
`MODULE_LEVELS` 为这些模块**收窄可选档位**，页面上就不渲染那些档——配得出来但
点了没用的开关，比没有这个开关更糟：它会让人以为自己给出去的是一个更小的权限。

收窄还顺带定义了 HTTP 方法的取整规则（见 `required_level`）：方法档位按
METHOD_LEVELS 推出来后，**向上取整到该模块最近的可选档**。所以 `agent` 模块
（none/read/write）上的 POST 直接要 write，不会落到一个该模块根本不存在的
`read_create` 上而被静默放行。取不到就 fail-closed 要 write。

## 独立能力位

按模块档位表达不了、又是高频诉求的，单独立字段，比塞进通用矩阵清楚得多：

- `can_execute`：可发起任务流执行；
- `can_exec_prod`：可对生产环境（Environment.is_prod）发起执行。

## 无角色的用户

严格回退到原来的 `is_staff` 语义，做到「谁的权限都没变」：

- staff → 全模块 write + 全能力位；
- 非 staff → 内置「普通运维」的**模块矩阵**（资产字典可查可增、其余只读），
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
#: 粒度 = 一块菜单 + 一个风险面（拆分判据见模块文档字符串）。
MODULES = {
    'cmdb': '服务器资产（服务器台账、服务器分配与服务规划）',
    'dict': '基础字典（项目组、渠道、环境、云厂商、服务类型）',
    'topo': '拓扑画布（接入链路图）',
    'exec': '任务执行（任务流定义、执行记录、执行日志与日志文件）',
    'method': '方法库（命令 / playbook / Jenkins 方法模板）',
    'variable': '执行变量（含敏感变量真值）',
    'artifact': '构建制品',
    'credential': '连接凭据（SSH 密码与私钥）',
    'cloud': '云控业务（域名解析、证书签发与部署）',
    'cloud_account': '云账号（AccessKey / Secret）',
    'agent': 'Agent 与执行通道',
    'account': '用户、角色与 API 凭证',
    'system': '系统配置',
    'audit': '操作审计',
}

#: 页面分组：角色页按这个顺序分块渲染（13 个模块平铺成一行行单选框没法看）。
#: 与侧边栏菜单同构，配角色的人不用在脑子里做一次「模块标识 → 我点哪个菜单」的翻译。
MODULE_GROUPS = (
    ('资产与配置', ('cmdb', 'dict', 'topo')),
    ('任务执行', ('exec', 'method', 'variable', 'artifact', 'credential')),
    ('云控管理', ('cloud', 'cloud_account')),
    ('系统管理', ('agent', 'account', 'system', 'audit')),
)

#: 模块的可选档位（未列出 = 四档全可选）。收窄理由逐条写在值后面。
MODULE_LEVELS = {
    # 审计表没有写接口，写档配了也只是个不生效的开关
    'audit': ('none', 'read'),
    # 制品只能由 CI 经 API 凭证上报，页面上没有「新增」，只有查与删
    'artifact': ('none', 'read', 'write'),
    # 新增一条方法 = 把一段任意命令带进平台，与改方法同等危险，不给中间档
    'method': ('none', 'read', 'write'),
    # read = 能在域名/证书页选到账号（AccessKey 会打码，Secret 本就不回显），
    # write = 能改 AK/SK。没有 read_create：新增账号就是引入一把新密钥
    'cloud_account': ('none', 'read', 'write'),
    # 用户 / 角色 / API 凭证是身份与提权面：读到的就是一份可攻击账号清单
    'account': ('none', 'write'),
    # 系统配置含 LDAP / SMTP 等连接口令，读即泄露
    'system': ('none', 'write'),
    # 建 Agent 会下发一次性 token、重置 token 能顶掉现网连接，新增不比修改轻
    'agent': ('none', 'read', 'write'),
}

#: HTTP 方法 → 所需最低权限档（再由 required_level 向上取整到模块可选档）
METHOD_LEVELS = {
    'GET': 'read', 'HEAD': 'read', 'OPTIONS': 'read',
    'POST': 'read_create',
    'PUT': 'write', 'PATCH': 'write', 'DELETE': 'write',
}

#: 内置角色（数据迁移据此播种，可在页面上改矩阵）。
#: `super` 等于原来的 is_staff=True；`ops_member` 等于原来的普通用户
#: （资产与字典可查可增、其余只读）；中间的 `ops_admin` 是二档模型缺的那一层。
#: 拆分模块后这里顺手收紧了两处：ops_admin 不再因为「能改域名」而连带能改
#: 云账号 AK/SK，也不再因为「能改任务流」而连带能改方法库之外的身份面。
BUILTIN_ROLES = (
    {
        'code': 'super', 'name': '超级管理', 'is_builtin': True,
        'remark': '全部模块读写，含用户/角色/凭据/云账号/API 凭证与生产执行',
        'can_execute': True, 'can_exec_prod': True,
        # 「最高档」而不是写死 write：audit 没有写接口（可选档只到 read），
        # 写死 write 会让内置角色一出生就带一个配不出来的档
        'permissions': {module: MODULE_LEVELS.get(module, LEVELS)[-1] for module in MODULES},
    },
    {
        'code': 'ops_admin', 'name': '运维管理', 'is_builtin': True,
        'remark': '日常运维读写；云账号/连接凭据/Agent 只读，用户、角色与系统配置不可见',
        'can_execute': True, 'can_exec_prod': True,
        'permissions': {
            'cmdb': 'write', 'dict': 'write', 'topo': 'write',
            'exec': 'write', 'method': 'write', 'variable': 'write', 'artifact': 'write',
            'cloud': 'write',
            'credential': 'read', 'agent': 'read', 'audit': 'read',
            'cloud_account': 'read', 'account': 'none', 'system': 'none',
        },
    },
    {
        'code': 'ops_member', 'name': '普通运维', 'is_builtin': True,
        'remark': '资产与字典可查可增；任务可执行但不能改定义、不能改方法、不能碰生产',
        'can_execute': True, 'can_exec_prod': False,
        'permissions': {
            'cmdb': 'read_create', 'dict': 'read_create', 'topo': 'read_create',
            'exec': 'read', 'method': 'read', 'variable': 'read', 'artifact': 'read',
            'cloud': 'read', 'audit': 'read',
            'credential': 'none', 'cloud_account': 'none',
            'agent': 'none', 'account': 'none', 'system': 'none',
        },
    },
)


def allowed_levels(module):
    """该模块页面上可选的档位；未收窄的模块就是全部四档。"""
    return MODULE_LEVELS.get(module, LEVELS)


def top_level(module):
    """该模块能配到的最高档（audit 是 read，其余多数是 write）。"""
    return allowed_levels(module)[-1]


def level_index(level):
    """权限档强弱序；未知档位当 none（fail-closed）。"""
    try:
        return LEVELS.index(level)
    except ValueError:
        return 0


def satisfies(level, required):
    return level_index(level) >= level_index(required)


def required_level(method, module=None):
    """该 HTTP 方法所需的最低档；未知方法当 write（fail-closed）。

    带 `module` 时向上取整到该模块的可选档：`agent` 只有 none/read/write，
    POST 推出来的 `read_create` 在那里不存在，不取整就会被 satisfies 判成
    「read 不够、write 够」——看着对，但只要将来有人给某模块留了 read_create
    之外的空档，就会出现「配不出来的档反而放行」。取不到更强的档一律要 write。
    """
    level = METHOD_LEVELS.get(str(method).upper(), 'write')
    if module is None:
        return level
    for candidate in allowed_levels(module):
        if satisfies(candidate, level):
            return candidate
    return 'write'


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
    return satisfies(user_level(user, module), required_level(method, module))


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


def _check_registry():
    """启动即校验注册表自洽：分组漏了模块、模块可选档写错，都在导入时就炸。

    这些表是「加个模块顺手改」的地方，漏改的表现全都是静默的——分组漏一个模块，
    页面上就永远配不到它（而它的默认档是 none，看起来像是「权限就该没有」）。
    """
    grouped = [module for _, members in MODULE_GROUPS for module in members]
    missing = sorted(set(MODULES) - set(grouped))
    extra = sorted(set(grouped) - set(MODULES))
    if missing or extra:
        raise RuntimeError(f'MODULE_GROUPS 与 MODULES 不一致：漏 {missing}，多 {extra}')
    if len(grouped) != len(set(grouped)):
        raise RuntimeError('MODULE_GROUPS 存在重复模块')
    for module, levels in MODULE_LEVELS.items():
        if module not in MODULES:
            raise RuntimeError(f'MODULE_LEVELS 含未登记模块：{module}')
        if 'none' not in levels or not set(levels) <= set(LEVELS):
            raise RuntimeError(f'模块 {module} 的可选档位非法：{levels}')
    for spec in BUILTIN_ROLES:
        for module, level in spec['permissions'].items():
            if level not in allowed_levels(module):
                raise RuntimeError(
                    f'内置角色 {spec["code"]} 的 {module} 档位 {level} 不在可选范围')


_check_registry()
