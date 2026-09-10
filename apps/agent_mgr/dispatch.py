"""runner → agent 的下发与等待。

run_via_agent 为同步阻塞接口（与 runner 每模块同步执行的语义一致）：
创建 AgentJob → 经 channel layer 组消息下发 → 轮询 AgentJob 至终态或超时。
日志分片由 AgentConsumer 异步落到 AgentJob.output，本函数终态后一次性取回。

需要 agent 回传的结构化终态（如 JumpServer 注册的逐台结果）时改用
run_via_agent_detail，多拿一份 AgentJob.result；run_via_agent 是它的薄壳。
"""
import logging
import time

from asgiref.sync import async_to_sync

from channels.layers import get_channel_layer

from django.utils import timezone

from .models import AgentJob

logger = logging.getLogger('apps.agent_mgr')

POLL_INTERVAL = 1  # 秒
# 落库 payload 里替换敏感字段的占位串（下发给 agent 的仍是原值）
SECRET_PLACEHOLDER = '***（敏感内容仅下发，未落库）***'
DISPATCH_MARGIN = 60  # 等待余量：模块 timeout 之外留给下发/回传的时间


def agent_offline_reason(agent, capability=None):
    """下发前置检查，返回不可下发原因（None = 可下发）。

    capability 非空时额外校验 agent 上报的能力清单（register 时落 meta）：
    老 agent 不上报 capabilities，此时不校验，由 agent 侧 task.ack 拒收兜底。
    """
    if not agent.enabled:
        return f'Agent「{agent.name}」已禁用。'
    if agent.status != 'online' or not agent.channel_name:
        return f'Agent「{agent.name}」离线（最近心跳：{agent.last_heartbeat or "从未"}），无法下发。'
    capabilities = (agent.meta or {}).get('capabilities')
    if capability and isinstance(capabilities, list) and capability not in capabilities:
        return (f'Agent「{agent.name}」（版本 {agent.version or "未知"}）不支持 '
                f'{capability} 执行单元，请升级 agent。当前能力：'
                f'{"、".join(capabilities) or "无"}。')
    return None


def redact_payload(payload, secret_fields):
    """返回可落库的 payload 副本：secret_fields 里的键值换成占位串。

    证书私钥这类内容必须下发给 agent，但不该留在 AgentJob.payload 里
    （任务详情/后台随手可见，DB 里也没有 EncryptedTextField 的保护）。
    """
    if not secret_fields:
        return payload
    redacted = dict(payload)
    for key in secret_fields:
        if key in redacted:
            redacted[key] = SECRET_PLACEHOLDER
    return redacted


def run_via_agent(agent, payload, timeout, **kwargs):
    """经 agent 执行一个模块单元，阻塞等待终态，返回 (rc, output)。

    参数见 run_via_agent_detail；只关心返回码与文本输出时用这个。
    """
    rc, output, _result = run_via_agent_detail(agent, payload, timeout, **kwargs)
    return rc, output


def run_via_agent_detail(agent, payload, timeout, task_id=None, log_id=None,
                         step_order=None, on_data=None, capability=None,
                         secret_fields=()):
    """同 run_via_agent，但额外返回 agent 上报的终态汇总，即 (rc, output, result)。

    通道层失败（离线/拒收/等待超时）rc=1/124，输出为中文原因，result 为 {}；
    执行结果 rc 与 agent 侧输出原样透传。on_data 为实时输出回调：轮询到新
    分片即回调增量（agent 侧日志分片由 consumer 异步落库）。capability 为
    该 payload 需要的 agent 能力（如 'jenkins'），用于对老 agent fail fast。
    secret_fields 列出只下发、不落库的 payload 键（如证书分发的 files）。
    result 即 AgentJob.result（含 host_results：JumpServer 注册的逐台结果）。
    """
    reason = agent_offline_reason(agent, capability=capability)
    if reason:
        return 1, reason, {}

    job = AgentJob.objects.create(
        agent=agent, payload=redact_payload(payload, secret_fields),
        task_id=task_id, log_id=log_id, step_order=step_order)
    payload = dict(payload, job_id=str(job.job_id))
    AgentJob.objects.filter(pk=job.pk).update(
        payload=redact_payload(payload, secret_fields))

    channel_layer = get_channel_layer()
    if channel_layer is None:
        _mark_failed(job, '未配置 channel layer（CHANNEL_LAYERS），无法下发。')
        return 1, 'agent 通道未就绪：未配置 channel layer（CHANNEL_LAYERS）。', {}
    try:
        async_to_sync(channel_layer.group_send)(
            agent.group_name, {'type': 'agent.dispatch', 'job': payload})
        AgentJob.objects.filter(pk=job.pk, status='pending').update(
            status='sent', updated_at=timezone.now())
    except Exception as exc:
        logger.exception('agent %s 下发失败：job=%s', agent.name, job.job_id)
        _mark_failed(job, f'下发失败：{exc}')
        return 1, f'agent 下发失败：{exc}', {}

    deadline = time.monotonic() + timeout + DISPATCH_MARGIN
    sent_len = 0
    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL)
        current = AgentJob.objects.filter(pk=job.pk).values(
            'status', 'return_code', 'output', 'error', 'result').first()
        if on_data:
            sent_len = _emit_delta(on_data, current['output'] or '', sent_len)
        if current['status'] in AgentJob.TERMINAL_STATUSES:
            if current['status'] == 'failed':
                return (current['return_code'] or 1,
                        current['error'] or 'agent 通道失败。', {})
            return current['return_code'], current['output'], current['result'] or {}

    # 等待超时：置失败并通知 agent 终止（agent 侧同时有自身超时兜底）
    _mark_failed(job, f'等待 agent 回传超时（>{timeout + DISPATCH_MARGIN}s）。', return_code=124)
    try:
        async_to_sync(channel_layer.group_send)(
            agent.group_name, {'type': 'agent.cancel', 'job_id': str(job.job_id)})
    except Exception:
        logger.exception('agent %s 取消指令下发失败：job=%s', agent.name, job.job_id)
    output = AgentJob.objects.filter(pk=job.pk).values_list('output', flat=True).first() or ''
    return (124,
            output + f'\n✖ 等待 agent 回传超时（>{timeout + DISPATCH_MARGIN}s），已通知 agent 终止。',
            {})


def _emit_delta(on_data, output, sent_len):
    """把 agent 已回传输出的新增部分喂给实时回调，返回新的已发送长度。

    output 被 agent 侧截断/重置（长度回退）时整体重发，避免拼出错位日志。
    """
    if len(output) < sent_len:
        sent_len = 0
    if len(output) > sent_len:
        try:
            on_data(output[sent_len:])
        except Exception:  # noqa: BLE001 - 回调失败不影响任务本体
            logger.exception('agent 输出实时回调失败：job 输出长度 %s', len(output))
        sent_len = len(output)
    return sent_len


def _mark_failed(job, error, return_code=1):
    AgentJob.objects.filter(pk=job.pk).exclude(
        status__in=AgentJob.TERMINAL_STATUSES,
    ).update(status='failed', return_code=return_code,
             error=error[:500], updated_at=timezone.now())
