"""飞书侧发起任务执行的准入判定。

**唯一入口**：飞书那边所有「能不能跑」的判断都走这里，不在 handlers/cards 里
各写一段。四道闸叠加，任一不过即拒：

1. **任务开关** `ExecutionTask.im_enabled` —— 默认关，开放哪几条任务是一次
   有意识的选择，不能全部挂在群里；
2. **默认全选主机** `default_select_all` —— 飞书里没法像执行弹窗那样逐模块勾
   主机；关掉默认全选的任务本就是要求人工确认，飞书不能绕过这条（与
   `task_exec/webhook.py` 的接口触发同一口径）；
3. **会话白名单** —— 未登记 / 停用 / 未授权执行的会话一律不给执行入口；
4. **人的权限** —— 直接复用 `task_exec.trigger.permission_error`（能力位 →
   生产环境 → 数据范围），**不另写一份**。飞书这条路最容易被当成「内部工具」
   而放松，它恰恰是最该跟页面一样严的。

**鉴权按点按钮的人判，不是按发卡片的人**：群里的卡片谁都看得见、按钮谁都点得到。
调用方必须先把 open_id 解析成平台 User（见 FeishuBinding），拿不到就直接拒。
"""
from apps.task_exec import trigger
from apps.task_exec.models import ExecutionTask

from .models import FeishuChannel


def available_tasks(user, chat_id=None):
    """该用户在该会话里可发起的任务查询集（供卡片的任务下拉用）。

    这里只做「列得出来」的收窄；真正发起前仍要过 `check()`——列表是按任务属性
    与数据范围筛的，能力位与生产那两道要针对具体任务再判一次。
    """
    qs = (ExecutionTask.objects
          .filter(im_enabled=True, default_select_all=True)
          .select_related('project_group', 'channel', 'environment')
          .order_by('name', 'id'))
    if chat_id is not None and not FeishuChannel.executable(chat_id):
        return qs.none()
    return user.narrow(qs)


def check(user, task, chat_id=None):
    """能否从飞书发起这条任务；返回中文拒绝原因，None 表示放行。"""
    if user is None:
        return '你的飞书账号尚未绑定平台用户，请联系管理员绑定后再操作。'
    if not task.im_enabled:
        return f'任务「{task.name}」未开启飞书执行，请在平台任务编辑页开启。'
    if not task.default_select_all:
        # 正常情况下序列化器已经拦住这种组合，这里兜存量数据与直接改库的情况
        return (f'任务「{task.name}」已关闭「默认全选主机」，需在页面上人工勾选主机后执行，'
                f'不支持飞书触发。')
    if chat_id is not None and not FeishuChannel.executable(chat_id):
        return '当前会话未被授权发起任务执行，请联系管理员在「飞书接入」中登记。'
    return trigger.permission_error(user, task)


def check_pause(user, task):
    """能否从飞书处理这次暂停；返回中文拒绝原因，None 表示放行。

    与发起执行的四道闸相比，**刻意不判会话白名单与任务开关**：白名单管的是
    「哪些会话能把执行入口摆出来」，而暂停卡是针对一次**已经发起**的执行，
    甚至可能是页面发起后私聊推给发起人的——那个会话不可能在白名单里。

    但人的权限一道不能少：continue / retry / skip 都会让剩余模块真的跑起来，
    只在发起那头拦等于留了个后门（页面上的 confirm 接口也是同一道口径）。
    """
    if user is None:
        return '你的飞书账号尚未绑定平台用户，请联系管理员绑定后再操作。'
    return trigger.permission_error(user, task)


def needs_production_confirm(task):
    """是否要走生产二次确认卡片。

    机器触发没有「弹窗再确认」这一步，把确认动作前移到卡片上（一次性 token、
    限发起人本人、60 秒有效），与接口触发要求 `confirm_production: true` 同一用意。
    """
    return bool(task.environment and task.environment.is_prod)
