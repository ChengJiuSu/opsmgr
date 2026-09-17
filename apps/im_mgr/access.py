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

from .models import FeishuApp, FeishuChannel


def session_allowed(chat_id=None, is_p2p=False):
    """这个会话能不能摆出执行入口。

    群/未知会话看白名单；**单聊看总开关**——菜单事件里压根没有 chat_id，
    没法登记，只能用 `FeishuApp.allow_p2p_execute` 表达同一个授权意思。
    """
    if is_p2p:
        return FeishuApp.load().allow_p2p_execute
    if chat_id is None:
        return True
    return FeishuChannel.executable(chat_id)


def available_tasks(user, chat_id=None, is_p2p=False):
    """该用户在该会话里可发起的任务查询集（供卡片的任务下拉用）。

    这里只做「列得出来」的收窄；真正发起前仍要过 `check()`——列表是按任务属性
    与数据范围筛的，能力位与生产那两道要针对具体任务再判一次。
    """
    qs = (ExecutionTask.objects
          .filter(im_enabled=True, default_select_all=True)
          .select_related('project_group', 'channel', 'environment')
          .order_by('name', 'id'))
    if not session_allowed(chat_id, is_p2p):
        return qs.none()
    return user.narrow(qs)


def check(user, task, chat_id=None, is_p2p=False):
    """能否从飞书发起这条任务；返回中文拒绝原因，None 表示放行。"""
    if user is None:
        return '你的飞书账号尚未绑定平台用户，请联系管理员绑定后再操作。'
    if not task.im_enabled:
        return f'任务「{task.name}」未开启飞书执行，请在平台任务编辑页开启。'
    if not task.default_select_all:
        # 正常情况下序列化器已经拦住这种组合，这里兜存量数据与直接改库的情况
        return (f'任务「{task.name}」已关闭「默认全选主机」，需在页面上人工勾选主机后执行，'
                f'不支持飞书触发。')
    if not session_allowed(chat_id, is_p2p):
        if is_p2p:
            return ('当前未开放「单聊发起执行」，请到群里操作，'
                    '或联系管理员在「飞书接入」中开启。')
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


def needs_dual_review(task):
    """这次执行要不要第二个人复核。

    拦的是「一个人在错误的时间对着错误的环境点了确认」——生产二次确认能让人看清
    在对什么下手，但看错了照样只有他自己知道。默认关：多一个人在线才能发版是真实
    代价，开不开由团队取舍。
    """
    mode = FeishuApp.load().dual_review
    if mode == 'all':
        return True
    return mode == 'prod' and needs_production_confirm(task)


def review_error(reviewer, task, initiator_id, chat_id=None):
    """复核人够不够格；返回中文拒绝原因，None 表示放行。

    **复核人自己也要满足发起执行的全部条件**（能力位、生产权限、数据范围）——
    否则「复核」只是找个人点一下，不构成第二道判断。

    比的是**平台用户**而不是飞书身份：换个飞书号绕不过去。
    """
    if reviewer is None:
        return '你的飞书账号尚未绑定平台用户，无法复核。'
    if reviewer.id == initiator_id:
        return '复核需要另一个人：发起人不能复核自己的执行。'
    return check(reviewer, task, chat_id)


def selectable_servers(task):
    """飞书上该列出来的主机：**各模块真正会用到的那些**。

    `task.matched_servers()` 只按三维度（项目组/渠道/环境）匹配，而服务类型是
    **模块级**的（`TaskStep.service_type`）——页面上的口径是「在全局范围里勾主机，
    每个模块再按自己的服务类型收窄」。照搬那个集合到卡片上，就会列出一批根本不会
    被任何模块执行的主机：勾了不生效，还让人以为会在上面跑。

    所以这里取**跟随全局范围的模块**按各自服务类型收窄后的**并集**：

    - `use_custom_scope=True` 的模块按自己的维度实时匹配，压根不吃勾选，
      它们的主机不该出现在多选里；
    - `pause` 模块不涉及主机。

    并集为空（比如所有模块都自定义了范围）时返回 None，调用方据此退回「整体执行」
    ——那种任务在飞书上勾主机没有任何意义。
    """
    steps = [s for s in task.steps.all()
             if s.task_type != 'pause' and not s.use_custom_scope]
    if not steps:
        return None
    global_servers = list(task.matched_servers())
    picked = {}
    for step in steps:
        for server in step.resolve_servers(global_servers):
            picked[server.id] = server
    return list(picked.values()) or None


def needs_production_confirm(task):
    """是否要走生产二次确认卡片。

    机器触发没有「弹窗再确认」这一步，把确认动作前移到卡片上（一次性 token、
    限发起人本人、60 秒有效），与接口触发要求 `confirm_production: true` 同一用意。
    """
    return bool(task.environment and task.environment.is_prod)
