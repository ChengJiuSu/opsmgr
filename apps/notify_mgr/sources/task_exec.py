"""任务执行（task_exec）接入消息通知。

订阅 `task_exec.signals.execution_updated`，把执行终态转成三种通知事件：

| 事件 | 时机 | 去重键 |
|------|------|--------|
| `task_exec.success` | 执行记录落 success | `log:{id}:success` |
| `task_exec.failed` | 执行记录落 failed（含进入「失败待处理」） | `log:{id}:failed:{结束时间}` |
| `task_exec.failure_closed` | 失败待处理被终止或超时收尾（最终失败） | `log:{id}:closed` |

失败的去重键带结束时间：失败待处理重试后**同一条执行记录**会再次结束，再失败
是一次新的失败，必须再通知；而同一次结束的重复广播（状态推送 + 模块表写入）
结束时间相同，被挡住。成功只会发生一次（成功后不能再续跑）。

任务上的「消息通知」开关（ExecutionTask.notify_enabled）关掉时三种事件都不发：
测试任务一天跑几十次，没人想在群里看见它们。
"""
import logging

from django.conf import settings

from .. import events

logger = logging.getLogger(__name__)

SOURCE_TYPE = 'task_exec.log'

EVENT_SUCCESS = 'task_exec.success'
EVENT_FAILED = 'task_exec.failed'
EVENT_CLOSED = 'task_exec.failure_closed'


# ── 事件发出：信号接收端 ──────────────────────────────────

def on_execution_updated(sender, task_id=None, log_id=None, kind='', **kwargs):
    """信号接收端：必须立刻返回，且不能抛异常。

    模块表刷新（kind=steps）是执行中的高频帧，终态一定会伴随一次状态推送，
    这里直接跳过 steps，省掉执行中每一帧的两次查库。
    """
    if kind == 'steps':
        return
    try:
        _handle(task_id, log_id, kind, kwargs)
    except Exception:  # noqa: BLE001 - 通知链路的任何问题都不该外溢到执行
        logger.warning('任务执行通知处理失败：task=%s log=%s', task_id, log_id, exc_info=True)


def _handle(task_id, log_id, kind, extra):
    from apps.task_exec.models import TaskExecutionLog

    qs = TaskExecutionLog.objects.select_related('task')
    if log_id is None:
        # push_status 只给 task_id：取该任务最近一次执行
        log = qs.filter(task_id=task_id).order_by('-created_at', '-id').first()
    else:
        log = qs.filter(pk=log_id).first()
    if log is None or not log.task.notify_enabled:
        return
    task = log.task
    dims = {'project_group': task.project_group_id, 'channel': task.channel_id,
            'environment': task.environment_id}
    key = f'{SOURCE_TYPE}:{log.pk}'

    if kind == 'failure_closed':
        events.emit(EVENT_CLOSED, source_type=SOURCE_TYPE, source_id=log.pk,
                    dedupe_key=f'{key}:closed', dims=dims,
                    context={'reason': extra.get('reason') or ''})
        return
    if log.status == 'success':
        events.emit(EVENT_SUCCESS, source_type=SOURCE_TYPE, source_id=log.pk,
                    dedupe_key=f'{key}:success', dims=dims)
    elif log.status == 'failed':
        finished = log.finished_at.timestamp() if log.finished_at else 0
        awaiting = bool(log.resume_state) and task.pause_type == 'failed'
        context = {'awaiting': awaiting}
        if awaiting and task.resume_deadline:
            context['deadline'] = task.resume_deadline.isoformat()
        events.emit(EVENT_FAILED, source_type=SOURCE_TYPE, source_id=log.pk,
                    dedupe_key=f'{key}:failed:{finished:.0f}', dims=dims, context=context)


# ── 消息渲染：发送时调用 ──────────────────────────────────

def _log_url(log):
    base = getattr(settings, 'SITE_BASE_URL', '').rstrip('/')
    return f'{base}/task/executions/{log.task_id}/logs/{log.id}' if base else None


def _user_name(user):
    if user is None:
        return '系统'
    return user.get_full_name() or user.username


def _fmt_time(value):
    from django.utils import timezone
    from django.utils.dateparse import parse_datetime

    if isinstance(value, str):
        value = parse_datetime(value)
    return timezone.localtime(value).strftime('%m-%d %H:%M') if value else ''


def build(event):
    """把任务执行事件渲染成通知消息。执行记录已被删除时返回 None。"""
    from apps.im_mgr import cards
    from apps.im_mgr.models import FeishuBinding, FeishuCardMessage
    from apps.task_exec.models import TaskExecutionLog

    log = (TaskExecutionLog.objects
           .select_related('task', 'task__project_group', 'task__channel',
                           'task__environment', 'triggered_by')
           .filter(pk=event.source_id).first())
    if log is None:
        return None
    task = log.task
    context = event.context or {}
    steps = log.steps_result or []

    if event.code == EVENT_SUCCESS:
        title, template, status_text = '✅ 任务执行成功', 'green', '执行成功'
    elif event.code == EVENT_CLOSED:
        title, template, status_text = '❌ 任务执行最终失败', 'red', '最终失败'
    else:
        title, template, status_text = '❌ 任务执行失败', 'red', '执行失败'

    elements = [
        cards.md(f'**{cards.esc(task.name)}** · {status_text}'),
        cards.md('\n'.join(cards.step_lines(steps)) or '（无模块）'),
    ]
    failed = [cards.esc(s.get('name') or f'模块{s.get("order")}')
              for s in steps if s.get('status') == 'failed']
    if failed and event.code != EVENT_SUCCESS:
        elements.append(cards.md(f'**失败模块**：{"、".join(failed)}'))
    if event.code == EVENT_FAILED:
        if context.get('awaiting'):
            deadline = _fmt_time(context.get('deadline'))
            elements.append(cards.md(
                f'⏸ **失败待处理**：{deadline + " 前" if deadline else "时限内"}'
                f'可在平台重试 / 跳过 / 终止，逾期按终止处理。'))
    elif event.code == EVENT_CLOSED:
        reason = context.get('reason') or '已终止'
        elements.append(cards.md(f'⛔ 失败待处理已收尾（{cards.esc(reason)}），不再重试。'))
    elements.append(cards.note(
        f'{cards.scope_line(task)}　{cards.env_tag(task)}　'
        f'{len(log.servers_snapshot or [])} 台主机　耗时 {cards.fmt_duration(log.duration)}'))
    elements.append(cards.note(
        f'{log.get_trigger_type_display()} · {cards.esc(_user_name(log.triggered_by))}'
        f'　{_fmt_time(log.finished_at)}'))
    if event.code != EVENT_SUCCESS:
        elements.append(cards.log_block(log.output))
    url = _log_url(log)
    if url:
        elements.append(cards.md(f'[查看执行详情]({url})'))

    mention = []
    if log.triggered_by_id:
        open_id = (FeishuBinding.objects.filter(user_id=log.triggered_by_id)
                   .values_list('open_id', flat=True).first())
        if open_id:
            mention.append(open_id)
    # 从飞书发起（或暂停时私聊推过卡片）的执行，那个会话里已经有一张实时刷新的
    # 卡片，会自己刷成终态，再发一条一模一样的是纯噪声
    bound = (FeishuCardMessage.objects.filter(log_id=log.pk)
             .values_list('chat_id', flat=True).first())

    return events.Message(
        title=title, elements=[e for e in elements if e], template=template,
        text=f'{title}：{task.name}（{cards.scope_line(task)}）',
        mention_open_ids=mention,
        exclude_targets={f'chat:{bound}'} if bound else set(),
    )


events.register(EVENT_SUCCESS, '任务执行成功', group='任务执行', builder=build)
events.register(EVENT_FAILED, '任务执行失败', group='任务执行', builder=build)
events.register(EVENT_CLOSED, '任务最终失败（失败待处理终止 / 超时）',
                group='任务执行', builder=build)
