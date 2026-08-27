"""任务流发起的公共前置校验与落库调度。

从 ExecutionTaskViewSet.execute 里抽出来，让「页面手动执行」与「接口/Webhook
触发」走同一段校验与快照逻辑：两条路径各写一份的话，早晚有一边漏掉服务规划
校验或主机快照，而这类漏检要到生产误执行时才暴露。

职责边界：本模块只做「校验 + 固化本次执行范围 + 交给调度」，主机的挑选口径
（逐模块勾选 / 全局兜底）留在调用方——页面来的选择结构复杂，接口来的只有
「全部匹配主机」一种。
"""
from .serializers import validate_service_planned
from .tasks import dispatch


def preflight(task):
    """发起前的任务级校验，返回中文错误消息；None 表示可以继续。"""
    if task.status in ('running', 'paused'):
        return '任务正在执行中，请等待完成后再发起'
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
    return dispatch(task, triggered_by=triggered_by, trigger_type=trigger_type), None


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
