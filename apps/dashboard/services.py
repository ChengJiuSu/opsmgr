"""概览页「值班视角」聚合。

页面回答三个问题，块的顺序就是这三问的顺序：
  1. 出事没？        → alerts（异常，无异常则整块为空）
  2. 有没有等我的？  → todos（不处理就一直停在那的东西）
  3. 现在在跑什么？  → running（执行中的任务与 agent 节点）

再加一块「盘子有多大」：assets（主机分布与服务规划状态）、artifacts（各维度当前
最新制品版本）。它们不是值班信号，放在页面下半部分。

口径上的两个决定：

- **实时算，不落表、不上定时任务。** 全是 count/exists 与小 limit 查询，数据量
  在十万级审计日志 + 千级任务的规模下毫秒返回；引入快照表就多一份会过期的真相。
- **块级权限在这里裁，不在前端裁。** 每块声明自己依赖的模块，`build_overview`
  按 `roles.user_level` 决定跑不跑。少了这一层，一个只有 cmdb 权限的账号能从
  首页读到证书名与内网域名——那正是各模块 module_perm 挡着的东西。

每个块带 `link`（前端路由 + query），明细条目能定位到更细的地方时再带自己的
`link`（如失败执行直接指向那一次的日志页）。由后端给是因为「哪条异常跳哪个
筛选」属于这块数据的语义，前端各自硬编码会散。
"""

import logging
from datetime import timedelta

from django.db.models import Count, Max, Q
from django.utils import timezone

from apps.agent_mgr.models import AgentNode
from apps.artifact_mgr.models import Artifact
from apps.asset_mgr.models import Server, ServerService
from apps.auth_mgr import roles
from apps.cloud_mgr.models import Certificate, CertDeployment, DnsChangeRequest, DnsRecord, Domain
from apps.task_exec.models import ExecutionTask, TaskExecutionLog
from apps.topo_mgr import health as topo_health
from apps.topo_mgr import impact as topo_impact
from apps.topo_mgr.models import TopoCanvas
from common.timeutil import local_dt

logger = logging.getLogger(__name__)

#: 证书进入「即将过期」的天数阈值。acme 续签窗口是 30 天，早于它告警只会天天红。
CERT_EXPIRY_WARN_DAYS = 30
#: 失败任务的回溯窗口：只看一个班次内的失败，更早的属于历史，去执行记录页翻。
TASK_FAILED_WINDOW = timedelta(hours=24)
#: 每块最多回传的明细条数：概览只给「点进去之前」的信息量。
ITEM_LIMIT = 5
#: 执行趋势的天数，与页面上的柱子数一致。
TREND_DAYS = 7


def _alert(key, level, title, items, link, total=None):
    """异常/待办块的统一形状。

    total 与 len(items) 分开：明细截断到 ITEM_LIMIT，但标题上的数字必须是全量，
    否则「3 张证书要过期」会在有 20 张时骗人。
    """
    return {
        'key': key,
        'level': level,
        'title': title,
        'total': len(items) if total is None else total,
        'items': items,
        'link': link,
    }


# ── 异常 ────────────────────────────────────────────────────────────


def cert_alerts():
    """证书过期 / 即将过期 / 申请失败。"""
    now = timezone.now()
    deadline = now + timedelta(days=CERT_EXPIRY_WARN_DAYS)
    link = {'path': '/cloud/certificates',
            'query': {'expiring_days': str(CERT_EXPIRY_WARN_DAYS)}}
    blocks = []

    expiring = list(
        Certificate.objects.filter(
            status='issued', expires_at__isnull=False, expires_at__lte=deadline,
        ).order_by('expires_at')[:ITEM_LIMIT + 1]
    )
    if expiring:
        total = Certificate.objects.filter(
            status='issued', expires_at__isnull=False, expires_at__lte=deadline).count()
        expired = [c for c in expiring if c.expires_at <= now]
        items = [{
            'id': c.id,
            'name': c.name,
            'expires_at': local_dt(c.expires_at),
            # 已过期时为负数，前端按正负分别显示「已过期 N 天 / 剩 N 天」
            'days_left': (c.expires_at - now).days,
            'auto_renew': c.auto_renew,
            'link': {'path': '/cloud/certificates', 'query': {'keyword': c.name}},
        } for c in expiring[:ITEM_LIMIT]]
        blocks.append(_alert(
            'cert_expiring',
            'danger' if expired else 'warning',
            f'{total} 张证书已过期或 {CERT_EXPIRY_WARN_DAYS} 天内到期',
            items, link, total=total,
        ))

    failed = list(Certificate.objects.filter(status='failed').order_by('-updated_at')[:ITEM_LIMIT])
    if failed:
        total = Certificate.objects.filter(status='failed').count()
        blocks.append(_alert(
            'cert_failed', 'danger', f'{total} 张证书申请失败',
            [{'id': c.id, 'name': c.name, 'error': (c.last_error or '')[:200],
              'link': {'path': '/cloud/certificates', 'query': {'keyword': c.name}}}
             for c in failed],
            {'path': '/cloud/certificates', 'query': {'status': 'failed'}}, total=total,
        ))
    return blocks


def cert_stuck_alerts():
    """签发 / 分发卡在中间态。

    issue_stuck、deploy_stuck 是模型上的 property（比较 updated_at 与各自的超时），
    没法进 SQL；先用 status 把候选缩到个位数，再在 Python 侧过滤。
    """
    stuck_certs = [c for c in Certificate.objects.filter(status='issuing') if c.issue_stuck]
    stuck_deploys = [
        d for d in CertDeployment.objects.filter(status='deploying').select_related('certificate')
        if d.deploy_stuck
    ]
    blocks = []
    if stuck_certs:
        blocks.append(_alert(
            'cert_issue_stuck', 'danger', f'{len(stuck_certs)} 张证书签发卡住（可重新申请）',
            [{'id': c.id, 'name': c.name, 'updated_at': local_dt(c.updated_at),
              'link': {'path': '/cloud/certificates', 'query': {'keyword': c.name}}}
             for c in stuck_certs[:ITEM_LIMIT]],
            {'path': '/cloud/certificates', 'query': {'status': 'issuing'}},
            total=len(stuck_certs),
        ))
    if stuck_deploys:
        blocks.append(_alert(
            'cert_deploy_stuck', 'danger', f'{len(stuck_deploys)} 个证书分发卡住（可重新分发）',
            # certificate_id / server_id 供拓扑深链反查用（明细本身按分发展示）
            [{'id': d.id, 'name': str(d), 'updated_at': local_dt(d.updated_at),
              'certificate_id': d.certificate_id, 'server_id': d.server_id,
              'link': {'path': '/cloud/certificates',
                       'query': {'keyword': d.certificate.name}}}
             for d in stuck_deploys[:ITEM_LIMIT]],
            {'path': '/cloud/certificates'},
            total=len(stuck_deploys),
        ))
    return blocks


def dns_alerts():
    """域名异常与解析同步失败。"""
    blocks = []
    abnormal = list(Domain.objects.filter(status='abnormal')[:ITEM_LIMIT])
    if abnormal:
        total = Domain.objects.filter(status='abnormal').count()
        blocks.append(_alert(
            'domain_abnormal', 'warning', f'{total} 个域名状态异常',
            [{'id': d.id, 'name': d.name,
              'link': {'path': '/cloud/domains', 'query': {'keyword': d.name}}}
             for d in abnormal],
            {'path': '/cloud/domains'}, total=total,
        ))

    failed = list(
        DnsRecord.objects.filter(sync_status='failed').select_related('domain')
        .order_by('-updated_at')[:ITEM_LIMIT]
    )
    if failed:
        total = DnsRecord.objects.filter(sync_status='failed').count()
        blocks.append(_alert(
            'dns_sync_failed', 'warning', f'{total} 条解析记录同步失败',
            [{'id': r.id, 'name': f'{r.rr}.{r.domain.name}', 'domain_id': r.domain_id,
              'link': {'path': '/cloud/domains', 'query': {'keyword': r.domain.name}}}
             for r in failed],
            {'path': '/cloud/domains'}, total=total,
        ))
    return blocks


def task_failed_alerts():
    """近 24 小时的失败执行。

    查执行日志表而不是 ExecutionTask.status：后者只是「最近一次执行」的快照，
    昨天失败、今天成功的任务在那上面看不出来，反过来也会把老失败一直挂着。
    """
    since = timezone.now() - TASK_FAILED_WINDOW
    qs = TaskExecutionLog.objects.filter(status='failed', created_at__gte=since)
    total = qs.count()
    if not total:
        return []
    logs = qs.select_related('task', 'triggered_by').order_by('-created_at')[:ITEM_LIMIT]
    return [_alert(
        'task_failed', 'danger', f'近 24 小时 {total} 次执行失败',
        [{
            'id': log.task_id,
            'log_id': log.id,
            'name': log.task.name if log.task_id else '(任务已删除)',
            'triggered_by': getattr(log.triggered_by, 'username', ''),
            'finished_at': local_dt(log.finished_at or log.created_at),
            # 直接指向那一次执行的日志页，不是任务列表：列表按任务的「最近一次
            # 执行」状态过滤，昨天失败今天成功的任务在那里根本筛不出来
            'link': {'path': f'/task/executions/{log.task_id}/logs/{log.id}'} if log.task_id else None,
        } for log in logs],
        {'path': '/task/executions'}, total=total,
    )]


def agent_alerts():
    """启用中但连不上的 agent。禁用的节点是人主动停的，不算异常。"""
    offline = [n for n in AgentNode.objects.filter(enabled=True) if n.is_offline]
    if not offline:
        return []
    return [_alert(
        'agent_offline', 'danger', f'{len(offline)} 个 Agent 离线',
        [{
            'id': n.id, 'name': n.name, 'last_heartbeat': local_dt(n.last_heartbeat),
            'link': {'path': '/system/agents'},
        } for n in offline[:ITEM_LIMIT]],
        {'path': '/system/agents'}, total=len(offline),
    )]


# ── 待处理 ──────────────────────────────────────────────────────────


def paused_todos():
    """卡在暂停确认 / 失败待处理的任务。

    平台里「不点就不会往下走」的东西，必须上首页：暂停确认模块还占着执行位在等；
    失败待处理已经释放了执行位，但过了 resume_deadline 就只能重新发起。
    """
    now = timezone.now()
    waiting = ExecutionTask.objects.filter(
        Q(status='paused') | Q(status='failed', pause_type='failed', resume_deadline__gt=now))
    tasks = list(
        waiting.select_related('created_by').order_by('-started_at')[:ITEM_LIMIT]
    )
    if not tasks:
        return []
    total = waiting.count()
    # 只为这几条取最近一条日志 id（执行页要靠它定位到具体那次执行的日志）
    log_map = {}
    for log in TaskExecutionLog.objects.filter(
            task_id__in=[t.id for t in tasks]).order_by('task_id', '-id'):
        log_map.setdefault(log.task_id, log.id)
    items = [{
        'id': t.id,
        'name': t.name,
        'pause_type': t.pause_type,
        'pause_type_label': t.get_pause_type_display() if t.pause_type else '',
        'log_id': log_map.get(t.id),
        'triggered_by': getattr(t.created_by, 'username', ''),
        # 已经等了多久：等待时间越长越可能是没人看见，前端按它排序高亮。失败待处理
        # 从失败那刻（finished_at）算，开跑时间早就过去很久了
        'waiting_seconds': _waiting_seconds(t, now),
        'link': ({'path': f'/task/executions/{t.id}/logs/{log_map[t.id]}'}
                 if log_map.get(t.id) else {'path': '/task/executions',
                                            'query': {'status': 'paused'}}),
    } for t in tasks]
    return [_alert(
        'task_paused', 'warning', f'{total} 个任务等待处理',
        items, {'path': '/task/executions', 'query': {'status': 'paused'}}, total=total,
    )]


def _waiting_seconds(task, now):
    since = task.finished_at if task.status == 'failed' else task.started_at
    return int((now - since).total_seconds()) if since else None


def dns_approval_todos(user):
    """待审批的 DNS 高危变更单（未过期的）。

    自己提交的也列出来并标记 self_submitted：后端禁止自批，藏起来会让申请人以为
    单子丢了，反而去开紧急通道。列出来 + 标注「需他人审批」才是对的。
    """
    now = timezone.now()
    qs = DnsChangeRequest.objects.filter(
        status=DnsChangeRequest.STATUS_PENDING, expires_at__gt=now)
    total = qs.count()
    if not total:
        return []
    reqs = qs.select_related('domain', 'requested_by').order_by('expires_at')[:ITEM_LIMIT]
    return [_alert(
        'dns_approval', 'warning', f'{total} 张 DNS 变更单待审批',
        [{
            'id': r.id,
            'name': f'{r.get_action_display()} {r.target_rr}.{r.domain.name}',
            'requested_by': getattr(r.requested_by, 'username', ''),
            'self_submitted': r.requested_by_id == user.id,
            'expires_at': local_dt(r.expires_at),
            'expires_in': int((r.expires_at - now).total_seconds()),
            'link': {'path': '/cloud/domains', 'query': {'requests': 'pending'}},
        } for r in reqs],
        {'path': '/cloud/domains', 'query': {'requests': 'pending'}}, total=total,
    )]


def mfa_todo(user):
    """自己还没启用二次认证。与权限无关，人人可见。"""
    mfa = getattr(user, 'mfa', None)
    if mfa is not None and mfa.enabled:
        return []
    return [_alert(
        'mfa_off', 'warning', '你还没有启用二次认证', [],
        {'path': '/security/mfa'}, total=1,
    )]


# ── 运行态 ──────────────────────────────────────────────────────────


def running_tasks():
    """正在执行的任务：跑了多久、到第几个模块、谁点的。"""
    now = timezone.now()
    logs = (
        TaskExecutionLog.objects.filter(status='running')
        .select_related('task', 'triggered_by').order_by('-started_at')[:10]
    )
    step_totals = dict(
        ExecutionTask.objects.filter(id__in=[log.task_id for log in logs])
        .annotate(c=Count('steps')).values_list('id', 'c')
    )
    return [{
        'id': log.task_id,
        'log_id': log.id,
        'name': log.task.name if log.task_id else '(任务已删除)',
        'triggered_by': getattr(log.triggered_by, 'username', ''),
        'started_at': local_dt(log.started_at),
        'elapsed': int((now - log.started_at).total_seconds()) if log.started_at else None,
        # steps_result 是「已跑完的模块」，当前正在跑的是它 +1 个
        'step_done': len(log.steps_result or []),
        'step_total': step_totals.get(log.task_id, 0),
    } for log in logs]


def agent_panel():
    """Agent 节点一览：心跳快照里的负载与磁盘现在只有 Agent 页看得到。"""
    nodes = list(AgentNode.objects.all()[:20])
    online = [n for n in nodes if not n.is_offline]
    return {
        'visible': True,
        'total': len(nodes),
        'online': len(online),
        'offline': len(nodes) - len(online),
        'nodes': [{
            'id': n.id,
            'name': n.name,
            'enabled': n.enabled,
            'status': 'online' if not n.is_offline else 'offline',
            'last_heartbeat': local_dt(n.last_heartbeat),
            'version': n.version,
            'load1': (n.meta or {}).get('load1'),
            'disk_free_pct': (n.meta or {}).get('disk_free_pct'),
            'running_jobs': (n.meta or {}).get('running_jobs'),
        } for n in nodes],
    }


# ── 资产盘子 ────────────────────────────────────────────────────────


#: 分布图最多列几档：环境/项目组超过这个数就把尾巴合成「其他」，
#: 首页不是报表页，二十条横条没人看。
DIST_LIMIT = 6


def _distribution(label_field):
    """按某个字典维度统计主机数，超出 DIST_LIMIT 的尾部合并为「其他」。"""
    rows = list(
        Server.objects.values(label_field).annotate(c=Count('id')).order_by('-c')
    )
    items = [{'name': r[label_field] or '未分配', 'count': r['c']} for r in rows]
    if len(items) > DIST_LIMIT:
        head, tail = items[:DIST_LIMIT], items[DIST_LIMIT:]
        head.append({'name': '其他', 'count': sum(i['count'] for i in tail)})
        items = head
    return items


def asset_summary():
    """主机分布与服务规划状态。

    不统计 Server.status（running/stopped/maintenance）：那是人工维护的字段，
    实际上没人在主机停机时回来改，长期恒为 running，摆在首页只是个假数字。
    真正有动作含义的是 ServerService 的三态——尤其「已规划未部署」，那是一批
    等着有人去部署的机器。
    """
    service_counts = dict(
        ServerService.objects.values_list('status').annotate(c=Count('id'))
    )
    return {
        'visible': True,
        'total': Server.objects.count(),
        'by_environment': _distribution('environment__name'),
        'by_project_group': _distribution('project_group__name'),
        'services': {
            'planned': service_counts.get('planned', 0),
            'deployed': service_counts.get('deployed', 0),
            'offline': service_counts.get('offline', 0),
        },
        'link': {'path': '/config/allocation'},
    }


def latest_artifacts():
    """各「项目组 × 渠道 × 环境 × 服务类型」的当前最新制品版本。

    「生产现在跑的是哪个版本」是运维被问最多的一句话，答案就在这张表里，但原先
    只能去制品页按维度筛。取数与 CD 注入 package_* 变量同口径（每组取 id
    最大的一条），不重算「最新」的定义——服务类型进分组正是因为注入也按它匹配：
    不分组的话同一环境里两个服务只会显示后上报的那一个版本，另一个服务在跑什么
    看不见。
    """
    latest_ids = list(
        Artifact.objects.values('project_group', 'channel', 'environment', 'service_type')
        .annotate(latest=Max('id')).values_list('latest', flat=True)
    )
    if not latest_ids:
        return {'visible': True, 'items': [], 'link': {'path': '/task/artifacts'}}
    rows = (
        Artifact.objects.filter(id__in=latest_ids)
        .select_related('project_group', 'channel', 'environment', 'service_type')
        .order_by('environment__id', 'project_group__id', 'channel__id', 'service_type__id')
    )
    return {
        'visible': True,
        'items': [{
            'id': a.id,
            'project_group': a.project_group.name,
            'channel': a.channel.name,
            'environment': a.environment.name,
            # 存量制品（服务类型上线前上报）给空串，前端显示「未指定」
            'service_type': a.service_type.name if a.service_type_id else '',
            'version': a.version,
            'build_number': a.build_number,
            'reported_at': local_dt(a.reported_at),
        } for a in rows],
        'link': {'path': '/task/artifacts'},
    }


# ── 执行趋势 ────────────────────────────────────────────────────────


def exec_trend():
    """近 7 天每天的执行成功 / 失败次数。

    页面原先那张趋势图画的是审计写操作条数：它把「改了一个字典项」和「往生产
    推了一次包」算作同样的一格，看不出这一周跑得顺不顺。执行次数按成败分色才
    是运维想从趋势里看的东西。

    未结束的执行（running/paused/pending）单独计一列，不混进成败——它们还没有
    结果，算进任何一边都会让当天的数字在几分钟后自己变。
    """
    today = timezone.localdate()
    start = today - timedelta(days=TREND_DAYS - 1)
    rows = (
        TaskExecutionLog.objects.filter(created_at__date__gte=start)
        .values_list('created_at__date', 'status').annotate(c=Count('id'))
    )
    buckets = {str(start + timedelta(days=i)): {'success': 0, 'failed': 0, 'running': 0}
               for i in range(TREND_DAYS)}
    for day, status, count in rows:
        bucket = buckets.get(str(day))
        if bucket is None:
            continue
        if status == 'success':
            bucket['success'] += count
        elif status == 'failed':
            bucket['failed'] += count
        else:
            bucket['running'] += count
    return {
        'visible': True,
        'days': [{'date': date, **counts} for date, counts in buckets.items()],
        'link': {'path': '/task/executions'},
    }


# ── 组装 ────────────────────────────────────────────────────────────

#: (块名, 依赖模块, builder, 是否要 user)。模块为 None = 不需要权限。
#: 异常块 → 该块明细对应哪些画布节点（kind, 取 id 的字段）。
#: 表驱动而不是在各 builder 里各写一遍：builder 关心的是「什么坏了」，
#: 「它画在哪张图上」是另一件事，混在一起两边都难改。
TOPO_REF_FIELDS = {
    'cert_expiring': (('certificate', 'id'),),
    'cert_failed': (('certificate', 'id'),),
    'cert_issue_stuck': (('certificate', 'id'),),
    'cert_deploy_stuck': (('certificate', 'certificate_id'), ('server', 'server_id')),
    'domain_abnormal': (('domain', 'id'),),
    # 解析记录同步失败时，图上可能只画了它的主域名而没画这条记录——两个都查，
    # 按画布去重。只查记录会让「明明画了这条链路」的图漏掉
    'dns_sync_failed': (('dns_record', 'id'), ('domain', 'domain_id')),
}


def attach_topo_links(blocks, user):
    """在异常明细上标出「它在哪张链路图上」，并给出深链。

    首页的异常是「什么坏了」，画布回答的是「坏的这个东西挂在哪条链路上」——两者之间
    原来要靠人自己去画布页翻。加这一层之后，值班的人从「证书要过期」一步跳到那张图，
    立刻能看见它罩着哪几台机。

    整个附加过程是**尽力而为**：一次批量查（`canvases_for_refs`），拿不到就不标，
    绝不因为它让异常块本身出不来——概览页是出事时最先被打开的页面。

    Agent 离线单独处理：Agent 本身不是画布上的节点类型，但钉死到它的主机是。看板上
    真正想知道的正是「这个 Agent 挂了，哪几条链路的机器现在下不了命令」。
    """
    if roles.user_level(user, 'topo') == 'none':
        return blocks
    canvases = TopoCanvas.objects.select_related(
        'project_group', 'channel', 'environment')
    if hasattr(user, 'narrow'):
        canvases = user.narrow(canvases)

    wanted = set()
    # 块内条目 → 该条目要查的 (kind, ref_id) 列表
    per_item = {}
    agent_servers = {}
    agent_ids = [
        item.get('id') for block in blocks if block['key'] == 'agent_offline'
        for item in block['items'] if item.get('id')
    ]
    if agent_ids:
        for server_id, agent_id in Server.objects.filter(
                channel_mode='agent', agent_id__in=agent_ids
        ).values_list('id', 'agent_id'):
            agent_servers.setdefault(agent_id, []).append(server_id)

    for block in blocks:
        for index, item in enumerate(block['items']):
            refs = []
            for kind, field in TOPO_REF_FIELDS.get(block['key'], ()):
                if item.get(field):
                    refs.append((kind, item[field]))
            if block['key'] == 'agent_offline':
                refs.extend(('server', sid)
                            for sid in agent_servers.get(item.get('id'), []))
            if refs:
                per_item[(block['key'], index)] = refs
                wanted.update(refs)

    if not wanted:
        return blocks
    found = topo_impact.canvases_for_refs(wanted, canvases)
    for block in blocks:
        for index, item in enumerate(block['items']):
            seen = {}
            for ref in per_item.get((block['key'], index), ()):
                for canvas in found.get(ref, ()):
                    seen[canvas['id']] = canvas
            if seen:
                item['topo'] = list(seen.values())
    return blocks


def topo_alerts(user):
    """拓扑画布上有异常节点的图。

    条目粒度是**画布**而不是节点：首页要回答的是「哪张链路图在冒烟」，逐节点列出来
    会把一台机的三个问题铺成三行，把别的异常挤出可见范围。点进去就是那张图，红的
    节点在图上一眼可见——这也是画布相对「一张异常清单」的全部价值。

    判定完全复用 topo_mgr 那套（同一份 issues 清单），不在这里另写 filter：第二套
    口径一定会和图上的颜色对不上，「首页说 3 个异常、图上只有 2 个红的」之后就没人
    再信这个数字。
    """
    canvases = TopoCanvas.objects.select_related(
        'project_group', 'channel', 'environment')
    # 数据范围与其它块同口径：看不到那张图的人也不该在首页看到它在报警
    if hasattr(user, 'narrow'):
        canvases = user.narrow(canvases)
    items, total = topo_health.alert_items(canvases, limit=ITEM_LIMIT)
    if not items:
        return []
    level = 'danger' if any(i['level'] == 'danger' for i in items) else 'warning'
    return [_alert(
        'topo_issues', level, f'{total} 张拓扑画布存在异常节点',
        items, {'path': '/topo/canvases', 'query': {}}, total=total,
    )]


_ALERT_BUILDERS = (
    ('cert_alerts', 'cloud', cert_alerts, False),
    ('cert_stuck_alerts', 'cloud', cert_stuck_alerts, False),
    ('dns_alerts', 'cloud', dns_alerts, False),
    ('task_failed_alerts', 'exec', task_failed_alerts, False),
    ('agent_alerts', 'agent', agent_alerts, False),
    ('topo_alerts', 'topo', topo_alerts, True),
)

_TODO_BUILDERS = (
    ('paused_todos', 'exec', paused_todos, False),
    ('dns_approval_todos', 'cloud', dns_approval_todos, True),
    ('mfa_todo', None, mfa_todo, True),
)


def _run(name, builder, user, needs_user):
    """跑一个 builder，异常吞掉只记日志。

    一块查挂了不能让整张首页 500——概览是出事时最先被打开的页面，它自己必须
    比任何单块数据都稳。
    """
    try:
        return builder(user) if needs_user else builder()
    except Exception:  # noqa: BLE001 - 单块失败降级为不显示
        logger.exception('概览块 %s 计算失败', name)
        return []


def build_overview(user):
    """按用户权限组装概览数据。"""
    def allowed(module):
        return module is None or roles.user_level(user, module) != 'none'

    alerts = []
    for name, module, builder, needs_user in _ALERT_BUILDERS:
        if allowed(module):
            alerts.extend(_run(name, builder, user, needs_user))
    # 附加拓扑深链：失败只记日志，异常块本身照常下发
    try:
        attach_topo_links(alerts, user)
    except Exception:  # noqa: BLE001 - 附加信息失败不影响主体
        logger.exception('概览块拓扑深链附加失败')

    todos = []
    for name, module, builder, needs_user in _TODO_BUILDERS:
        if allowed(module):
            todos.extend(_run(name, builder, user, needs_user))

    running = {'visible': allowed('exec'), 'tasks': []}
    if running['visible']:
        running['tasks'] = _run('running_tasks', running_tasks, user, False)

    agents = {'visible': False, 'total': 0, 'online': 0, 'offline': 0, 'nodes': []}
    if allowed('agent'):
        result = _run('agent_panel', agent_panel, user, False)
        if result:
            agents = result

    assets = {'visible': False, 'total': 0, 'by_environment': [],
              'by_project_group': [], 'services': {}}
    if allowed('cmdb'):
        assets = _run('asset_summary', asset_summary, user, False) or assets

    artifacts = {'visible': False, 'items': []}
    if allowed('artifact'):
        artifacts = _run('latest_artifacts', latest_artifacts, user, False) or artifacts

    trend = {'visible': False, 'days': []}
    if allowed('exec'):
        trend = _run('exec_trend', exec_trend, user, False) or trend

    return {
        'alerts': alerts, 'todos': todos, 'running': running, 'agents': agents,
        'assets': assets, 'artifacts': artifacts, 'trend': trend,
    }
