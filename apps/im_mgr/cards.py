"""飞书消息卡片模板。

**纯函数，无副作用**：入参是普通对象，出参是可直接 `json.dumps` 的 dict。
发卡片、改卡片、落 message_id 都在别处（client.py / tasks.py），这里不碰数据库、
不发请求——卡片是这套交互里最容易反复改的部分，保持可单测、改起来才不用提心吊胆。

按钮回传值（`value`）的约定：`{'act': 动作, ...}`，动作常量就在本模块顶部，
handlers 从这里 import，两边不会各写一份字符串。value 会原样回到回调里，
**只放标识不放状态**：卡片可能被翻出来重点，状态必须以库里为准。

三条硬约束（都吃过亏的地方）：

- **用户内容一律走 `esc()`**：任务名里一个 `*` 或 `` ` `` 就能把整张卡片的
  markdown 结构带歪，轻则样式乱，重则后面的内容被吞掉；
- **日志尾段要剥反引号**：贴进 ``` 代码块的日志里如果自带 ```，围栏会提前闭合，
  剩下的日志直接当 markdown 渲染；
- **模块列表要截断**：卡片有大小上限，一条 50 个模块的任务流全渲染出来会超。
"""

# ── 按钮动作常量（handlers 与本模块共用）──
ACT_PICK_TASK = 'pick_task'       # 下拉选中一条任务
ACT_RUN = 'run'                   # 确认执行（非生产）
ACT_CONFIRM_PROD = 'confirm_prod'  # 生产二次确认
ACT_CANCEL = 'cancel'             # 取消
ACT_PAUSE = 'pause'               # 暂停处理（continue/abort/retry/skip）

#: 模块列表最多渲染这么多条，其余折成一行说明
MAX_STEPS_SHOWN = 20
#: 卡片里贴的日志尾段行数
LOG_TAIL_LINES = 20
#: 单行日志过长时截断（避免一行几千字符把卡片撑爆）
LOG_LINE_LIMIT = 300

STEP_ICONS = {
    'pending': '⬜',
    'running': '⏳',
    'success': '✅',
    'failed': '✖',
    'skipped': '⏭',
}
STATUS_ICONS = {
    'pending': '⬜',
    'running': '⏳',
    'paused': '⏸',
    'success': '✅',
    'failed': '✖',
}


# ── 基础构件 ──────────────────────────────────────────────

def esc(text):
    """转义用户内容里的 markdown 记号。

    任务名、模块名、主机名都是人填的，`*bold*`、`` `code` ``、`[link](x)` 这些
    记号会把卡片结构带歪。飞书的 lark_md 不支持反斜杠转义，所以用全角同形字符
    替换——看起来一样，不参与渲染。
    """
    if text is None:
        return ''
    table = str.maketrans({'*': '＊', '_': '＿', '`': '｀', '~': '～',
                           '[': '［', ']': '］'})
    return str(text).translate(table)


def md(content):
    """一段 lark_md 文本。"""
    return {'tag': 'div', 'text': {'tag': 'lark_md', 'content': content}}


def note(content):
    """底部小字说明。"""
    return {'tag': 'note', 'elements': [{'tag': 'plain_text', 'content': content}]}


def hr():
    return {'tag': 'hr'}


def button(text, value, style='default', confirm=None):
    """一个按钮。`style`：default / primary / danger。"""
    element = {
        'tag': 'button',
        'text': {'tag': 'plain_text', 'content': text},
        'type': style,
        'value': value,
    }
    if confirm:
        # 飞书原生的点击确认弹窗。生产那道**不靠它**：它只是客户端弹窗，
        # 拦不住重放，真正的确认是带一次性 token 的第二张卡片
        element['confirm'] = {
            'title': {'tag': 'plain_text', 'content': confirm[0]},
            'text': {'tag': 'plain_text', 'content': confirm[1]},
        }
    return element


def actions(*buttons):
    return {'tag': 'action', 'actions': [b for b in buttons if b]}


def card(title, elements, template='blue'):
    """组装一张卡片。"""
    return {
        'config': {'wide_screen_mode': True, 'update_multi': True},
        'header': {
            'title': {'tag': 'plain_text', 'content': title},
            'template': template,
        },
        'elements': elements,
    }


def fmt_duration(seconds):
    if seconds is None:
        return '—'
    seconds = int(seconds)
    if seconds < 60:
        return f'{seconds}s'
    if seconds < 3600:
        return f'{seconds // 60}m{seconds % 60:02d}s'
    return f'{seconds // 3600}h{seconds % 3600 // 60:02d}m'


def log_block(text):
    """把日志尾段包成代码块。

    先剥掉反引号：日志里自带 ``` 会让围栏提前闭合，后面的内容就当 markdown 渲染
    出去了（曾经把一段堆栈渲染成了标题）。
    """
    if not text:
        return None
    lines = [line[:LOG_LINE_LIMIT] for line in str(text).splitlines()][-LOG_TAIL_LINES:]
    body = '\n'.join(lines).replace('`', "'").strip()
    if not body:
        return None
    return md(f'```\n{body}\n```')


def scope_line(task):
    """任务的维度范围一行文案。"""
    parts = [getattr(task.project_group, 'name', None),
             getattr(task.channel, 'name', None),
             getattr(task.environment, 'name', None)]
    return ' / '.join(esc(p) for p in parts if p) or '未配置范围'


def is_prod(task):
    return bool(task.environment and task.environment.is_prod)


def env_tag(task):
    return '🔴 生产' if is_prod(task) else '🟢 非生产'


# ── 交互卡片 ──────────────────────────────────────────────

def task_picker(tasks, *, tip=None, p2p=False):
    """任务选择卡：下拉列出该用户在本会话可发起的任务。

    列表为空时不渲染下拉——一个点开只有「无选项」的下拉比一句说明更让人困惑。
    """
    elements = [md('选择要执行的任务流：')]
    if tasks:
        options = [{
            'text': {'tag': 'plain_text', 'content': f'{t.name}（{scope_line(t)}）'[:100]},
            'value': str(t.id),
        } for t in tasks]
        elements.append(actions({
            'tag': 'select_static',
            'placeholder': {'tag': 'plain_text', 'content': '请选择任务'},
            'options': options,
            # 场景随按钮一路带下去：卡片回调的 context 里**没有 chat_type**，而
            # 单聊的 chat_id 同样是 oc_ 开头，从 id 上分不出群聊还是单聊
            #（照前缀猜的后果是单聊卡片全被当成未授权的群）
            'value': {'act': ACT_PICK_TASK, 'p2p': p2p},
        }))
    else:
        elements = [md('**当前没有可从飞书发起的任务。**')]
        elements.append(note('任务需在平台上开启「允许从飞书发起执行」，'
                             '且当前会话已被授权发起执行。'))
    if tip:
        elements.append(note(tip))
    return card('运维任务执行', elements)


def task_detail(task, server_count, step_count, *, token=None, p2p=False):
    """任务确认卡：展示本次要跑什么，按钮发起执行。"""
    elements = [
        md(f'**{esc(task.name)}**'),
        md(f'**范围**：{scope_line(task)}　{env_tag(task)}\n'
           f'**模块**：{step_count} 个，按序执行，失败中止后续\n'
           f'**主机**：当前匹配 {server_count} 台'),
    ]
    if server_count == 0:
        # 让人点下去再看到「没有主机」的报错，不如现在就说清楚
        elements.append(note('当前范围未匹配到可执行主机，发起会被拒绝。'))
        elements.append(actions(button('重新选择', {'act': ACT_CANCEL})))
        return card('确认执行', elements, template='grey')

    run_button = button(
        '⚠ 执行到生产' if is_prod(task) else '执行',
        {'act': ACT_RUN, 'task': task.id, 'token': token, 'p2p': p2p},
        style='danger' if is_prod(task) else 'primary',
    )
    elements.append(actions(run_button, button('取消', {'act': ACT_CANCEL})))
    if is_prod(task):
        elements.append(note('目标是生产环境，点击后还需再确认一次。'))
    return card('确认执行', elements, template='red' if is_prod(task) else 'blue')


def production_confirm(task, server_count, step_count, token, ttl_seconds, *, p2p=False):
    """生产二次确认卡。

    机器触发没有「弹窗再确认」这一步，把确认动作前移到这张卡上：一次性 token、
    限发起人本人、短有效期。文案把**环境名与主机数写死**——确认的意义在于让人
    看清自己在对什么下手，只写「确认执行吗」等于没确认。
    """
    elements = [
        md(f'**即将对生产环境执行：{esc(task.name)}**'),
        md(f'**环境**：{esc(getattr(task.environment, "name", ""))}\n'
           f'**范围**：{scope_line(task)}\n'
           f'**影响**：{step_count} 个模块 × {server_count} 台主机'),
        hr(),
        actions(
            button('确认执行到生产',
                   {'act': ACT_CONFIRM_PROD, 'task': task.id, 'token': token, 'p2p': p2p},
                   style='danger'),
            button('取消', {'act': ACT_CANCEL}),
        ),
        note(f'仅发起人本人可确认，{ttl_seconds} 秒内有效，过期请重新发起。'),
    ]
    return card('⚠ 生产执行确认', elements, template='red')


# ── 状态卡片 ──────────────────────────────────────────────

def step_lines(steps):
    """逐模块状态行（含截断）。"""
    lines = []
    for item in (steps or [])[:MAX_STEPS_SHOWN]:
        icon = STEP_ICONS.get(item.get('status'), '⬜')
        name = esc(item.get('name') or f'模块{item.get("order")}')
        extra = ''
        if item.get('status') in ('success', 'failed') and item.get('duration') is not None:
            extra = f'（{fmt_duration(item["duration"])}）'
        lines.append(f'{icon} {name}{extra}')
    remaining = len(steps or []) - MAX_STEPS_SHOWN
    if remaining > 0:
        lines.append(f'… 还有 {remaining} 个模块')
    return lines


def progress(task, log, *, url=None):
    """执行进度卡：随 steps_result 变化刷新同一条消息。"""
    steps = log.steps_result or []
    icon = STATUS_ICONS.get(log.status, '⏳')
    elements = [
        md(f'{icon} **{esc(task.name)}** · {log.get_status_display()}'),
        md('\n'.join(step_lines(steps)) or '（尚未开始）'),
        note(f'{scope_line(task)}　{env_tag(task)}　'
             f'{len(log.servers_snapshot or [])} 台主机'),
    ]
    if url:
        elements.append(md(f'[查看完整日志]({url})'))
    return card('任务执行中', elements, template='wathet')


def result(task, log, *, url=None, tail=None):
    """终态卡：成功/失败的定稿，失败时附日志尾段。"""
    ok = log.status == 'success'
    steps = log.steps_result or []
    failed = [esc(s.get('name')) for s in steps if s.get('status') == 'failed']
    elements = [
        md(f'{STATUS_ICONS.get(log.status, "")} **{esc(task.name)}** · {log.get_status_display()}'),
        md('\n'.join(step_lines(steps)) or '（无模块）'),
        note(f'{scope_line(task)}　{env_tag(task)}　'
             f'{len(log.servers_snapshot or [])} 台主机　耗时 {fmt_duration(log.duration)}'),
    ]
    if failed:
        elements.insert(2, md(f'**失败模块**：{"、".join(failed)}'))
    if not ok:
        block = log_block(tail)
        if block:
            elements.append(block)
    if url:
        elements.append(md(f'[查看完整日志]({url})'))
    return card('执行完成' if ok else '执行失败', elements,
                template='green' if ok else 'red')


#: 暂停类型 → (按钮文案, 动作值, 样式)
PAUSE_BUTTONS = {
    'confirm': [('继续执行', 'continue', 'primary'), ('中止', 'abort', 'danger')],
    'failed': [('重试该模块', 'retry', 'primary'),
               ('跳过继续', 'skip', 'default'),
               ('中止', 'abort', 'danger')],
}


def pause(task, log, pause_type, *, current_step=None, tail=None, url=None):
    """暂停处理卡：直接在飞书里处理「等待确认」与「失败待处理」。

    这是整套接入里收益最大的一张卡——在此之前，任务卡在 paused 必须有人盯着
    页面。失败分支附日志尾段，不开页面就能判断该重试还是中止。
    """
    kind = pause_type if pause_type in PAUSE_BUTTONS else 'confirm'
    title = '任务等待确认' if kind == 'confirm' else '模块执行失败，等待处理'
    elements = [
        md(f'⏸ **{esc(task.name)}**'),
        md(f'**当前模块**：{esc(current_step) or "—"}\n'
           f'**范围**：{scope_line(task)}　{env_tag(task)}'),
    ]
    if kind == 'failed':
        block = log_block(tail)
        if block:
            elements.append(block)
    elements.append(actions(*[
        button(text, {'act': ACT_PAUSE, 'task': task.id, 'log': log.id, 'do': value},
               style=style)
        for text, value, style in PAUSE_BUTTONS[kind]
    ]))
    if url:
        elements.append(md(f'[查看完整日志]({url})'))
    return card(title, elements, template='orange')


def handled(text, *, by=None):
    """动作已处理后的定格卡：把按钮换成结论，避免同一张卡被反复点。"""
    content = f'✅ {esc(text)}'
    if by:
        content += f'　·　处理人：{esc(by)}'
    return card('已处理', [md(content)], template='grey')


def notice(title, content, *, template='grey', tip=None):
    """通用提示卡（拒绝原因、参数错误等）。"""
    elements = [md(esc(content))]
    if tip:
        elements.append(note(tip))
    return card(title, elements, template=template)
