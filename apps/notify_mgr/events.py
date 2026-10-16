"""通知事件注册表与发出口：其他功能接入消息通知只走这里。

接一种新事件只要两步，通知中心其余部分（规则匹配、去重、发送、记录）不用动：

    # 1. 注册（模块导入时，一般在 sources/<来源>.py）
    events.register('workflow.failed', '工作流执行失败', group='工作流', builder=build_workflow)

    # 2. 事情发生时发出（必须立刻返回，且不抛异常）
    events.emit('workflow.failed', source_type='workflow.run', source_id=run.id,
                dedupe_key=f'workflow.run:{run.id}:failed',
                dims={'project_group': run.project_group_id, ...})

`builder(event) -> Message | None` 在**发送时**调用（异步 worker 里），现读库现
渲染：异步投递之间没有顺序保证，渲染推迟到发送那一刻，晚到的也是最新状态。
返回 None 表示这条已经不该发了（比如来源对象被删了）。
"""
import logging
from dataclasses import dataclass, field

from django.db import IntegrityError, transaction

logger = logging.getLogger(__name__)


@dataclass
class Message:
    """一条待发送的通知，与具体发送通道无关。

    `elements` 是飞书卡片 2.0 的组件列表（目前两种通道都是飞书）；`text` 是
    纯文本摘要，给以后不支持卡片的通道（邮件、短信）兜底用。
    """

    title: str
    elements: list
    template: str = 'blue'
    text: str = ''
    #: 可被 @ 的人（飞书 open_id），规则开了「@ 发起人」才会带上
    mention_open_ids: list = field(default_factory=list)
    #: 跳过的目标键（NotifyRule.target_key）：比如那个会话里已经有一张实时刷新的卡片
    exclude_targets: set = field(default_factory=set)


@dataclass
class EventType:
    code: str
    label: str
    group: str
    builder: object
    #: 新建规则时是否默认勾选
    default: bool = True


REGISTRY = {}


def register(code, label, *, group, builder, default=True):
    REGISTRY[code] = EventType(code=code, label=label, group=group,
                               builder=builder, default=default)


def get(code):
    return REGISTRY.get(code)


def choices():
    """给前端的事件清单（按注册顺序，同组相邻）。"""
    return [{'code': e.code, 'label': e.label, 'group': e.group, 'default': e.default}
            for e in REGISTRY.values()]


def label_of(code):
    event_type = REGISTRY.get(code)
    return event_type.label if event_type else code


def emit(code, *, source_type, source_id, dedupe_key, dims=None, context=None):
    """发出一次事件：落库（按 dedupe_key 去重）并异步投递。

    返回新建的 NotifyEvent；重复事件或出错返回 None。**绝不抛异常**——调用方
    多半在业务主链路上，通知出问题不能把业务带停。
    """
    from . import tasks
    from .models import NotifyEvent

    if code not in REGISTRY:
        logger.warning('未注册的通知事件：%s', code)
        return None
    dims = dims or {}
    try:
        # 包一层保存点：调用方可能在事务里，撞唯一约束不能把外层事务一起弄坏
        with transaction.atomic():
            event = NotifyEvent.objects.create(
                code=code, dedupe_key=dedupe_key[:200],
                source_type=source_type, source_id=str(source_id),
                project_group_id=dims.get('project_group'),
                channel_id=dims.get('channel'),
                environment_id=dims.get('environment'),
                context=context or {},
            )
    except IntegrityError:
        # 同一件事已经发过（终态会被重复广播），唯一约束挡住第二次
        return None
    except Exception:  # noqa: BLE001 - 通知链路的任何问题都不该外溢到业务
        logger.warning('通知事件落库失败：%s %s', code, dedupe_key, exc_info=True)
        return None
    tasks.dispatch_deliver(event.pk)
    return event
