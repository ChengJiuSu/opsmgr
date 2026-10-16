"""任务执行的对外通知信号。

给**本模块之外**的订阅者用（目前是飞书卡片刷新）。存在的理由是依赖方向：
task_exec 不该知道 im_mgr 的存在——那会让「装不装飞书」变成执行链路的编译期
依赖，而飞书发不出去时任务执行必须照跑。

发送点刻意与 WebSocket 推送同处（live.publish_steps / live.push_status）：那两处
本来就是「执行状态对外广播」的收口，逐模块状态与终态都必经。**不用 post_save
信号**——模块表是用 queryset `.update()` 写的（避开整行保存），post_save 根本
不会触发。

约定：接收方只拿到 id，要什么自己去库里读。信号可能在执行线程/worker 里同步
触发，接收方必须立刻返回、把活儿丢给异步，且不能抛异常。
"""
from django.dispatch import Signal

#: 一次执行的状态或模块表发生了变化。
#: kwargs：task_id、log_id（可能为 None，接收方按任务取最近一条）、kind：
#:   steps / status —— 模块表 / 执行状态变化；
#:   failure_closed —— 失败待处理被终止或超时收尾（另带 reason）。执行状态本来
#:   就是 failed、收尾不改状态，订阅方只看状态分不出「刚失败」与「最终失败」
execution_updated = Signal()


def notify(task_id, log_id=None, kind='', **extra):
    """广播一次执行更新；任何订阅者异常都不得影响执行本身。"""
    import logging

    logger = logging.getLogger(__name__)
    try:
        execution_updated.send(sender=None, task_id=task_id, log_id=log_id, kind=kind, **extra)
    except Exception:  # noqa: BLE001 - 订阅者出问题不该把任务执行带停
        logger.warning('执行更新通知失败：task=%s log=%s kind=%s',
                       task_id, log_id, kind, exc_info=True)
