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
ACT_PICK_HOSTS = 'pick_hosts'     # 展开主机多选（默认全选）
ACT_RUN_SELECTED = 'run_selected'  # 表单提交：按勾选的主机执行
ACT_PROBE = 'probe'               # 组件探针：回显飞书实际回传的 action 结构

#: 主机多选组件里最多列这么多台。超过就只给「全部执行」并提示去页面挑——
#: 下拉塞几百个选项既卡片超限也没法用
MAX_HOSTS_SELECTABLE = 50
#: 表单里多选组件的 name，回传时按它从 form_value 里取
HOSTS_FIELD = 'hosts'

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
    """一段 markdown 文本。

    2.0 用 `markdown` 标签，1.0 的 `div` + `lark_md` 嵌套没了。
    """
    return {'tag': 'markdown', 'content': content}


def note(content):
    """小字说明。

    **2.0 删掉了 `note` 标签**（`cards of schema V2 no longer support this
    capability`），只能退回普通 markdown。保留这个函数是为了让调用处仍能表达
    「这是附注」的意图。
    """
    return {'tag': 'markdown', 'content': content}


def hr():
    return {'tag': 'hr'}


def button(text, value, style='default'):
    """一个按钮。`style`：default / primary / danger。

    2.0 的回调值放在 `behaviors`，1.0 的顶层 `value` 不再被接受——飞书的卡片
    JSON 是严格校验，多一个未知属性整张卡都发不出去，所以不能两个都写。
    """
    return {
        'tag': 'button',
        'text': {'tag': 'plain_text', 'content': text},
        'type': style,
        'behaviors': [{'type': 'callback', 'value': value}],
    }


def submit_button(text, value, style='primary', name='submit'):
    """表单提交按钮（必须放在 `form` 容器里）。

    比普通按钮多 `form_action_type` 与 `name`：提交时飞书会把表单内所有组件的值
    一起带回（`action.form_value`），这正是 1.0 做不到、非升 2.0 不可的那件事。
    """
    element = button(text, value, style=style)
    element['name'] = name
    element['form_action_type'] = 'submit'
    return element


def form(name, elements):
    """表单容器。"""
    return {'tag': 'form', 'name': name, 'elements': [e for e in elements if e]}


def actions(*elements):
    """把若干交互组件横向排成一行。

    2.0 没有 1.0 的 `action` 容器，横排靠 `column_set`。单个组件时不套容器——
    少一层嵌套，出错时 ErrPath 也短一截。
    """
    kept = [e for e in elements if e]
    if not kept:
        return None
    if len(kept) == 1:
        return kept[0]
    return {
        'tag': 'column_set',
        'horizontal_spacing': '8px',
        'columns': [{'tag': 'column', 'width': 'auto', 'elements': [e]} for e in kept],
    }


def card(title, elements, template='blue'):
    """组装一张卡片（**schema 2.0**）。

    **一条消息的卡片版本不能中途切换**：在 1.0 消息的回调里返回 2.0 卡片（反之
    亦然）会被飞书拒掉，客户端上表现为「出错了，请稍后重试 code: 200673」或者
    干脆点了没反应。所以整套统一 2.0，没有混用的余地。
    """
    return {
        'schema': '2.0',
        'config': {'width_mode': 'fill', 'update_multi': True},
        'header': {
            'title': {'tag': 'plain_text', 'content': title},
            'template': template,
        },
        'body': {
            'direction': 'vertical',
            'elements': [e for e in elements if e],
        },
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
        elements.append({
            'tag': 'select_static',
            'placeholder': {'tag': 'plain_text', 'content': '请选择任务'},
            'options': options,
            # 场景随组件一路带下去：卡片回调的 context 里**没有 chat_type**，而
            # 单聊的 chat_id 同样是 oc_ 开头，从 id 上分不出群聊还是单聊
            #（照前缀猜的后果是单聊卡片全被当成未授权的群）
            'behaviors': [{'type': 'callback',
                           'value': {'act': ACT_PICK_TASK, 'p2p': p2p}}],
        })
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
        f'⚠ 执行到生产（全部 {server_count} 台）' if is_prod(task)
        else f'执行（全部 {server_count} 台）',
        {'act': ACT_RUN, 'task': task.id, 'token': token, 'p2p': p2p},
        style='danger' if is_prod(task) else 'primary',
    )
    # 「选择主机」是可选支路：不点就是全部执行，默认路径一步都没多
    pick_button = (button('选择主机', {'act': ACT_PICK_HOSTS, 'task': task.id, 'p2p': p2p})
                   if server_count > 1 else None)
    elements.append(actions(run_button, pick_button, button('取消', {'act': ACT_CANCEL})))
    if server_count > MAX_HOSTS_SELECTABLE:
        elements.append(note(f'主机超过 {MAX_HOSTS_SELECTABLE} 台，'
                             f'飞书里只能整体执行；要挑主机请到平台页面操作。'))
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


# ── 主机勾选（表单）──────────────────────────────────────

def host_select(task, servers, *, p2p=False, selected=None):
    """主机多选卡：表单容器一次把勾选结果带回来。

    用表单而不是让多选自己回调：1.0 时代的交互组件各自独立回调，点「执行」时拿不到
    之前选了什么，只能在服务端暂存选择状态——多一份会过期、会串会话的状态。这是
    整套升 2.0 的直接动因。

    默认**全部勾上**：默认路径（不点「选择主机」）与这里的默认值一致，改动才是例外。
    `selected_values` 是唯一被接受的默认选中写法，`initial_options` 会被严格校验
    直接拒掉。
    """
    servers = list(servers)[:MAX_HOSTS_SELECTABLE]
    values = [str(s.id) for s in servers]
    chosen = [str(v) for v in (selected if selected is not None else values)]
    options = [{
        'text': {'tag': 'plain_text',
                 'content': f'{s.name} · {getattr(s, "conn_ip", "") or ""}'[:100]},
        'value': str(s.id),
    } for s in servers]
    return card(
        '选择执行主机' + ('（生产）' if is_prod(task) else ''),
        [
            md(f'**{esc(task.name)}**\n{scope_line(task)}　{env_tag(task)}'),
            form('host_form', [
                {
                    'tag': 'multi_select_static',
                    'name': HOSTS_FIELD,
                    'placeholder': {'tag': 'plain_text', 'content': '选择要执行的主机'},
                    'options': options,
                    'selected_values': chosen,
                },
                submit_button(
                    '执行所选主机',
                    {'act': ACT_RUN_SELECTED, 'task': task.id, 'p2p': p2p},
                    style='danger' if is_prod(task) else 'primary',
                ),
            ]),
            note('默认全选；取消勾选即可只跑部分主机。'
                 + ('生产环境仍需再确认一次。' if is_prod(task) else '')),
        ],
        template='red' if is_prod(task) else 'blue',
    )


# ── 组件探针 ──────────────────────────────────────────────

def probe_multi_select(options, *, preselect=None):
    """探针卡：表单 + 多选 + 提交按钮，用来问清飞书实际怎么回传选中值。

    真机问出来的三条 2.0 规则（都写进了对应的构件）：

    - 默认选中只认 `selected_values`，`initial_options` 报 `unknown property`；
    - **没有 `note` 标签**：`cards of schema V2 no longer support this capability`；
    - 一条消息的卡片版本**不能中途切换**，回调返回的卡片必须同版本。

    飞书的卡片 JSON 是严格校验且**逐个报错**，每次只告诉你第一个问题，所以别为了
    「兼容」往里塞备选字段——塞了就是下一次报错。
    """
    preselect = list(preselect if preselect is not None else [o['value'] for o in options])
    choices = [{'text': {'tag': 'plain_text', 'content': o['label']}, 'value': o['value']}
               for o in options]
    return card('组件探针（多选 · 卡片 2.0）', [
        md('先在下拉里改几项，再点「提交」。'),
        form('probe_form', [
            {
                'tag': 'multi_select_static',
                'name': HOSTS_FIELD,
                'placeholder': {'tag': 'plain_text', 'content': '选择主机（默认全选）'},
                'options': choices,
                'selected_values': preselect,
            },
            submit_button('提交', {'act': ACT_PROBE, 'from': 'form'}),
        ]),
        note('这张卡只用于确认飞书回传的数据结构，不会执行任何任务。'),
    ], template='grey')


def probe_result(action):
    """把飞书回传的 action 原样展示出来。"""
    import json as _json

    body = _json.dumps(action, ensure_ascii=False, indent=2)[:1500].replace('`', "'")
    return card('探针回传', [
        md('飞书这次回传的 action：'),
        md(f'```\n{body}\n```'),
    ], template='grey')
