"""数据范围（项目组 / 环境）的判定与收窄，用户与 API 凭证共用一份实现。

两个主体（登录用户 `User`、机读凭证 `ApiCredential`）都要按「可访问项目组 /
可访问环境」限制，语义必须完全一致——两份实现迟早分叉，而分叉的表现是「页面上
看不到的主机，用 API key 却能读到」。

范围一律**纯收窄**：空集合 = 不限，配不出比原权限更宽的东西。这是它比「路径
通配符授权」安全的根本原因——多选框表达不出「过宽」。
"""


def allows_dims(dims, project_group_id=None, environment_id=None):
    """该维度组合是否在范围内。

    入参为 None 表示「本次不涉及该维度」，不做限制：按项目组查主机时没带环境，
    不该因为限了环境就整体拒绝；真正的收窄由 narrow() 做。
    """
    groups, envs = dims
    if groups is not None and project_group_id is not None and project_group_id not in groups:
        return False
    if envs is not None and environment_id is not None and environment_id not in envs:
        return False
    return True


def narrow(queryset, dims, project_group_field='project_group_id',
           environment_field='environment_id'):
    """按范围收窄查询集。

    收窄可见集而不是返回 403：调用方忘了带筛选参数时拿到的是「我的全部」，
    不是「全公司的全部」。
    """
    groups, envs = dims
    if groups is not None:
        queryset = queryset.filter(**{f'{project_group_field}__in': groups})
    if envs is not None:
        queryset = queryset.filter(**{f'{environment_field}__in': envs})
    return queryset


def scope_label(dims):
    """范围的可读描述（日志与报错里指名道姓，省一次翻页面）。"""
    groups, envs = dims
    parts = []
    if groups is not None:
        parts.append(f'项目组 {sorted(groups)}')
    if envs is not None:
        parts.append(f'环境 {sorted(envs)}')
    return ' + '.join(parts) if parts else '不限'
