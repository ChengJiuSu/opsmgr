"""基于角色矩阵的模块权限类（替代二档的 IsAdmin / IsAdminOrReadOnly / …）。

档位、模块清单与「为什么不做成 CAM 策略语言」见 apps/auth_mgr/roles.py。

映射关系（换过来后行为不回退）：

| 原来 | 现在 |
|------|------|
| `IsAdmin`（连读都只给管理员）| `module_perm('x', min_level='write')` |
| `IsAdminOrReadOnly` | `module_perm('x')` |
| `IsAdminOrReadCreate` | `module_perm('x')` |
| `IsAuthenticated`（业务只读页）| `module_perm('x')` |

`module_perm('x')` 按 HTTP 方法要求档位（GET→read、POST→read_create、
改删→write），所以「可读可新增」这一档能自然表达出来——那正是 CMDB 现在的语义。

`min_level` 用于「整个 ViewSet 连读都要最高档」的场景（连接凭据、系统配置、
API 凭证）：这些接口的读本身就泄露敏感信息，不能因为是 GET 就放行。
"""
from rest_framework.permissions import BasePermission

from . import roles


class HasModulePerm(BasePermission):
    """要求用户在指定模块具备足够权限档。用 module_perm() 生成子类。"""

    #: 子类覆盖
    module = None
    #: 子类覆盖：抬高门槛（None = 按 HTTP 方法推导）
    min_level = None

    def has_permission(self, request, view):
        user = getattr(request, 'user', None)
        if not (user and user.is_authenticated):
            return False
        required = self.min_level or roles.required_level(request.method)
        return roles.satisfies(roles.user_level(user, self.module), required)


def module_perm(module, min_level=None):
    """生成模块权限类。

    模块名拼错在这里就报错：写成类属性的话，要到线上返回 403 才发现——而且因为
    未登记模块的档位一律是 none，表现是「谁都进不去」，很容易被当成权限配错。
    """
    if module not in roles.MODULES:
        raise ValueError(f'未登记的模块：{module}，请先在 roles.MODULES 中登记。')
    if min_level is not None and min_level not in roles.LEVELS:
        raise ValueError(f'非法权限档：{min_level}')
    name = f'HasModulePerm{module.title()}' + (f'{min_level.title()}' if min_level else '')
    return type(name, (HasModulePerm,), {'module': module, 'min_level': min_level})


class HasCapability(BasePermission):
    """要求独立能力位（can_execute / can_exec_prod）。用 capability_required() 生成。"""

    capability = None
    message = '当前角色没有该操作的权限。'

    def has_permission(self, request, view):
        user = getattr(request, 'user', None)
        if not (user and user.is_authenticated):
            return False
        return roles.user_capability(user, self.capability)


def capability_required(capability):
    if capability not in ('can_execute', 'can_exec_prod'):
        raise ValueError(f'未知能力位：{capability}')
    return type(f'Has{capability.title()}', (HasCapability,), {'capability': capability})
