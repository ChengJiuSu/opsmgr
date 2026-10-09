"""任务流发起的公共前置校验与落库调度。

从 ExecutionTaskViewSet.execute 里抽出来，让「页面手动执行」与「接口/Webhook
触发」走同一段校验与快照逻辑：两条路径各写一份的话，早晚有一边漏掉服务规划
校验或主机快照，而这类漏检要到生产误执行时才暴露。

职责边界：本模块只做「校验 + 固化本次执行范围 + 交给调度」，主机的挑选口径
（逐模块勾选 / 全局兜底）留在调用方——页面来的选择结构复杂，接口来的只有
「全部匹配主机」一种。
"""
from apps.auth_mgr import roles

from .exec_queue import queued_log
from .host_lock import mutex
from .serializers import validate_service_planned
from .tasks import create_log, schedule


def permission_error(user, task):
    """发起/处理任务执行的权限校验，返回中文错误消息；None 表示放行。

    三道：能力位（可否发起执行）→ 生产环境（可否碰生产）→ 数据范围（能碰哪些项目）。

    生产单独立一个能力位而不是塞进模块矩阵：「能改任务定义」与「能往生产推」是两件
    事，前者错了可以改回来，后者错了已经推上去了。这也是最高频的真实诉求。

    放在 trigger 而不是 views：页面执行、飞书卡片（apps/im_mgr）都要用同一段判定，
    各写一份早晚有一边漏掉生产那道。
    """
    if not roles.user_capability(user, 'can_execute'):
        return '当前角色没有发起任务执行的权限。'
    environment = task.environment
    if environment is not None and environment.is_prod \
            and not roles.user_capability(user, 'can_exec_prod'):
        return f'当前角色不允许对生产环境（{environment.name}）发起执行。'
    if not user.allows_dims(task.project_group_id, task.environment_id):
        return f'该任务不在你的数据范围内（当前范围：{user.scope_label}）。'
    return None


#: 暂停类型 -> 允许的处理动作（'' 兼容存量：视为暂停确认）
RUNNING_STATUSES = ('running', 'paused')


def is_active(task):
    """任务是否处在一次执行的发起到终态之间（期间不允许再发起/编辑）。

    排队中也算：排队或等主机锁期间再点一次，会给同一任务投出第二条执行，两条共用
    同一份任务快照字段（状态/输出），面板上就分不清谁是谁。但 status=pending 本身
    不能当排队——从没执行过的任务默认也是 pending，要看有没有未开跑的执行记录。
    """
    if task.status in RUNNING_STATUSES:
        return True
    return task.status == 'pending' and queued_log(task) is not None

PAUSE_ACTIONS = {
    'confirm': ('continue', 'abort'),
    'failed': ('retry', 'skip', 'abort'),
}


def apply_pause_action(task_id, action):
    """人工处理一次暂停：写入 pause_action 交给 runner 消费。

    返回中文错误消息；None 表示已写入。与 preflight/launch 同处的理由一样——
    页面与飞书卡片共用这一段，各写一份早晚有一边漏掉「动作与暂停类型是否匹配」
    这道校验，而那会让 failed 状态收到一个 continue 信号。

    状态判定用条件更新（`status='paused'`）而不是先查后写：两个人同时点、或者
    runner 刚好消费完信号，条件更新是唯一能保证只生效一次的写法。
    """
    from .models import ExecutionTask

    if action not in ('continue', 'abort', 'retry', 'skip'):
        return 'action 必须为 continue/abort（暂停确认）或 retry/skip/abort（失败处理）'
    row = ExecutionTask.objects.filter(pk=task_id).values_list('status', 'pause_type').first()
    if row is None or row[0] != 'paused':
        return '任务当前不在「等待处理」状态，无法操作'
    allowed = PAUSE_ACTIONS[row[1] or 'confirm']
    if action not in allowed:
        return f'当前暂停类型仅支持动作：{"/".join(allowed)}'
    updated = ExecutionTask.objects.filter(pk=task_id, status='paused').update(pause_action=action)
    if not updated:
        return '任务当前不在「等待处理」状态，无法操作'
    return None


def preflight(task):
    """发起前的任务级校验，返回中文错误消息；None 表示可以继续。"""
    if task.status in RUNNING_STATUSES:
        return '任务正在执行中，请等待完成后再发起'
    if is_active(task):
        return '任务正在排队等待执行，请等待完成后再发起'
    if not task.steps.exists():
        return '任务未配置任何模块，请先编辑任务添加模块'
    # 兜底存量任务：主机范围三维度必选，未选齐不予执行
    if not (task.project_group_id and task.channel_id and task.environment_id):
        return '任务未配置完整的主机范围（项目组/渠道/环境必选），请先编辑任务补全'
    return None


def launch(task, steps, selected_global, picked, triggered_by, trigger_type='execute'):
    """逐模块校验 → 固化主机快照 → 重置任务状态 → 调度执行。

    返回 (log, error)：error 非空时未做任何写入，调用方直接回失败。
    """
    error = validate_steps(steps, selected_global, picked)
    if error:
        return None, error
    # 抢占发起权：preflight 是先查后写，两个人同时点（或连点两下）都能过。
    # 「是否在执行」要看执行记录（见 is_active），一条条件 UPDATE 表达不了，
    # 所以用按任务的互斥把「复查 + 写快照 + 建执行记录」串起来；投递放在锁外——
    # sync 后端的投递就是整个执行过程
    with mutex(f'task_exec:launch:{task.pk}') as got:
        if not got:
            return None, '任务正在发起中，请稍后再试'
        task.refresh_from_db()
        if is_active(task):
            return None, '任务正在排队或执行中，请等待完成后再发起'
        log = _snapshot_and_log(task, steps, selected_global, picked, triggered_by, trigger_type)
    schedule(task, log)
    return log, None


def _snapshot_and_log(task, steps, selected_global, picked, triggered_by, trigger_type):
    """固化本次执行范围、重置任务快照，并建出 pending 的执行记录。"""
    # 固化主机快照：模块级勾选落到各模块，任务级存并集（日志/兜底口径）
    for step in steps:
        if step.use_custom_scope or step.task_type == 'pause':
            step.servers.clear()
        else:
            step.servers.set(picked.get(step.id, selected_global))
    snapshot = {}
    for step in steps:
        if step.use_custom_scope or step.task_type == 'pause':
            continue
        snapshot.update({s.id: s for s in picked.get(step.id, selected_global)})
    task.servers.set(snapshot.values())
    task.status = 'pending'
    task.return_code = None
    task.output = ''
    task.pause_action = ''  # 清残留处理信号，避免上次未消费的动作误触发
    task.pause_type = ''
    task.started_at = None
    task.finished_at = None
    task.save(update_fields=[
        'status', 'return_code', 'output', 'pause_action', 'pause_type',
        'started_at', 'finished_at',
    ])
    return create_log(task, triggered_by=triggered_by, trigger_type=trigger_type)


def validate_steps(steps, selected_global, picked):
    """逐模块解析主机并校验执行条件（暂停确认模块不涉及主机）。"""
    for step in steps:
        if step.task_type == 'pause':
            continue
        servers = step.resolve_servers(picked.get(step.id, selected_global))
        if not servers:
            scope = '自定义范围维度未匹配到可执行主机' if step.use_custom_scope \
                else '全局范围未匹配/未勾选任何主机'
            if step.service_type_id:
                scope += f'（服务类型「{step.service_type.name}」过滤后为空）'
            return f'模块「{step.name}」{scope}，无法执行'
        if step.task_type == 'method':
            error = validate_service_planned(step.method, servers)
            if error:
                return f'模块「{step.name}」：{error}'
    return None
