"""执行进度回显：把一次执行的状态变化刷到那张飞书卡片上。

订阅 `task_exec.signals.execution_updated`（在 live.publish_steps /
live.push_status 处广播），把「该刷卡片了」这件事异步丢出去。

四条硬规矩，每条都对应一个会真出事的场景：

- **绝不在执行线程里发 HTTP**。信号是在 ansible 执行的线程/worker 里同步触发的，
  在那里发飞书请求 = 飞书抖一下任务就慢一拍，飞书挂了任务卡在那儿。
  这里只做节流判断与投递，出站全在 tasks.py 的异步后端。
- **渲染在发送时做，不在投递时做**。异步之间没有顺序保证，一条早投递的进度帧
  如果带着当时的快照晚到，会把终态卡片覆盖回「执行中」。所以只传 log_id，
  worker 落地时现读库现渲染——晚到的帧渲染出来也是终态。
- **终态不节流**。中间态刷丢一帧无所谓（下一帧就补上了），终态丢了就永远停在
  「执行中」，而那正是唯一有人会回头看的状态。
- **失败只记日志**。飞书发不出去不回写任何任务状态：页面上的执行记录始终是
  权威来源，卡片只是通知。
"""
import logging

from django.conf import settings
from django.core.cache import cache

from . import cards, tasks
from .models import FeishuApp, FeishuBinding, FeishuCardMessage

logger = logging.getLogger(__name__)

#: 中间态两次刷新之间的最小间隔（秒）。模块多的任务流状态变化很密，
#: 不节流会把飞书接口打成限流，卡片反而更新不上
THROTTLE_SECONDS = 3
THROTTLE_PREFIX = 'feishu:cardupd:'
#: 这些状态一律立刻刷，不吃节流
ALWAYS_PUSH = ('success', 'failed', 'paused')


def bind(log, chat_id, message_id):
    """记住这次执行对应哪张卡片。"""
    if not (chat_id and message_id):
        return None
    obj, _ = FeishuCardMessage.objects.update_or_create(
        log=log, defaults={'chat_id': chat_id, 'message_id': message_id})
    return obj


def on_execution_updated(sender, task_id=None, log_id=None, kind='', **kwargs):
    """信号接收端。必须立刻返回，且不能抛异常。

    失败待处理收尾（kind=failure_closed）紧跟在一次状态推送之后，卡片已经按
    那次推送刷过了，这里不再重复刷。
    """
    if kind == 'failure_closed':
        return
    try:
        _schedule(task_id, log_id)
    except Exception:  # noqa: BLE001 - 通知链路的任何问题都不该外溢到执行
        logger.warning('飞书卡片刷新调度失败：task=%s log=%s', task_id, log_id, exc_info=True)


def _schedule(task_id, log_id):
    from apps.task_exec.models import TaskExecutionLog

    if log_id is None:
        # push_status 只给 task_id：取该任务最近一次执行
        log_id = (TaskExecutionLog.objects.filter(task_id=task_id)
                  .order_by('-created_at', '-id').values_list('id', flat=True).first())
    if log_id is None:
        return
    status = (TaskExecutionLog.objects.filter(pk=log_id)
              .values_list('status', flat=True).first())
    if not FeishuCardMessage.objects.filter(log_id=log_id).exists():
        # 没绑卡片的执行（页面发起的）平时不打扰：只有转「等待处理」/「失败待处理」
        # 时才私聊推给发起人——需要人处理是唯一值得主动打断的时刻
        if status == 'paused' or (status == 'failed' and _awaiting_failure(log_id)):
            tasks.dispatch_pause_push(log_id)
        return
    if not _should_push(log_id, status):
        return
    tasks.dispatch_refresh_card(log_id)


def _awaiting_failure(log_id):
    """这次执行是否停在「失败待处理」（失败已落终态，但还能重试/跳过/终止）。"""
    from apps.task_exec.models import TaskExecutionLog

    return TaskExecutionLog.objects.filter(
        pk=log_id, status='failed', resume_state__isnull=False,
        task__pause_type='failed').exists()


def pending_pause_type(log):
    """这次执行等人处理的类型：'confirm' / 'failed'；不需要处理时为 None。

    暂停确认时执行还在跑（status=paused）；失败待处理时执行已落 failed、断点
    挂在执行记录上——后者看着是终态，但卡片上还得摆出重试/跳过/终止。
    """
    if log.status == 'failed' and log.resume_state and log.task.pause_type == 'failed':
        return 'failed'
    if log.status == 'paused':
        return log.task.pause_type or 'confirm'
    return None


def _should_push(log_id, status):
    """节流判断；终态一律放行。"""
    if status in ALWAYS_PUSH:
        return True
    try:
        # cache.add 只在键不存在时成功 = 距上次刷新已超过节流窗口
        return bool(cache.add(THROTTLE_PREFIX + str(log_id), 1, timeout=THROTTLE_SECONDS))
    except Exception:  # noqa: BLE001 - 缓存不可用时照常刷（fail-open）
        # 这里刻意与 dedupe 的 fail-closed 相反：丢掉的只是一次界面刷新，
        # 多刷几次的代价远小于卡片停在半路
        logger.warning('卡片刷新节流缓存不可用，按不节流处理', exc_info=True)
        return True


def log_url(log):
    """平台上这次执行的详情地址。"""
    base = getattr(settings, 'SITE_BASE_URL', '').rstrip('/')
    return f'{base}/task/executions/{log.task_id}/logs/{log.id}' if base else None


def current_step_name(log, pause_type):
    """暂停时停在哪个模块上。"""
    wanted = 'failed' if pause_type == 'failed' else 'running'
    for item in (log.steps_result or []):
        if item.get('status') == wanted:
            return item.get('name') or f'模块{item.get("order")}'
    return ''


def render(log):
    """按当前状态渲染卡片。"""
    task = log.task
    url = log_url(log)
    # 暂停类型在任务本体上（处理后即清），日志里没有
    pause_type = pending_pause_type(log)
    if pause_type is None and log.status in ('success', 'failed'):
        # tail 用库里的输出预览：它就是完整日志的尾部，够判断成败原因
        return cards.result(task, log, url=url, tail=log.output)
    if pause_type:
        return cards.pause(
            task, log, pause_type,
            current_step=current_step_name(log, pause_type),
            # 失败待处理才贴日志：要判断该重试还是中止，靠的就是这几行
            tail=log.output if pause_type == 'failed' else None,
            url=url)
    return cards.progress(task, log, url=url)


PAUSE_PUSH_PREFIX = 'feishu:pausepush:'


def push_pause(log_id):
    """页面发起的任务卡在「等待处理」时，私聊推一张处理卡给发起人。

    推完就把这条消息绑到本次执行上：后续的进度与终态照常就地刷新，于是私聊里
    也始终是同一条消息在变，不会因为一次暂停而多出一串消息。

    **只推一次**：同一次执行可能反复进入等待处理（失败重试后再次失败），
    缓存标记挡住重复推送；真正的去重是绑定关系——绑上之后就走刷新那条路了。
    """
    from apps.task_exec.models import TaskExecutionLog

    log = (TaskExecutionLog.objects
           .select_related('task', 'task__project_group', 'task__channel',
                           'task__environment', 'triggered_by')
           .filter(pk=log_id).first())
    if log is None or pending_pause_type(log) is None or log.triggered_by is None:
        return
    if FeishuCardMessage.objects.filter(log_id=log_id).exists():
        return
    if not FeishuApp.load().is_ready():
        return

    binding = FeishuBinding.objects.filter(user=log.triggered_by).first()
    if binding is None or not binding.open_id:
        return
    try:
        if not cache.add(PAUSE_PUSH_PREFIX + str(log_id), 1, timeout=3600):
            return
    except Exception:  # noqa: BLE001 - 缓存不可用时照推（绑定关系会挡住第二次）
        logger.warning('暂停推送去重缓存不可用，按未推送处理', exc_info=True)

    message_id = tasks.send_card(binding.open_id, render(log), receive_id_type='open_id')
    if message_id:
        bind(log, binding.open_id, message_id)


def refresh(log_id):
    """真正发出去（异步后端里跑）。"""
    binding = (FeishuCardMessage.objects
               .select_related('log', 'log__task', 'log__task__project_group',
                               'log__task__channel', 'log__task__environment')
               .filter(log_id=log_id).first())
    if binding is None:
        return
    if not FeishuApp.load().is_ready():
        return
    log = binding.log
    # 现读现渲染：晚到的帧也会渲染出当前（可能已是终态）的样子
    log.refresh_from_db()
    tasks.update_card(binding.message_id, render(log))
