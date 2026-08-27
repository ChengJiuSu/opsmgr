"""解析记录高危变更的审批流：建单、审批、执行。

与 dns_history（留痕/回滚）的分工：这里管「改之前要不要有人点头」，那边管
「改之后能不能撤回」。两者都要——审批防改错，留痕防改错之后，而且回滚不依赖
任何人在线。

执行前有两道额外检查，都是这类系统的典型事故来源：

- **过期作废**（settings.DNS_APPROVAL_TTL_HOURS）；
- **漂移比对**：申请时记下的线上值与执行时的现值不一致（期间有人改过）就拒绝，
  要求重新提交。光靠过期时间挡不住 24 小时内的漂移，而闭着眼睛把陈旧目标值推
  上去，等于用审批流的名义做了一次没人审过的变更。
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from . import dns_history, dns_risk
from .models import DnsChangeRequest, DnsRecord
from .providers import DnsProviderError, get_dns_provider

logger = logging.getLogger(__name__)

#: 变更单 payload 里允许的字段（多余字段一律丢弃，避免把 request.data 原样落库）
PAYLOAD_FIELDS = ('rr', 'type', 'value', 'ttl', 'line', 'enabled', 'protected',
                  'target_type', 'server_id', 'remark')


def approval_enabled():
    return bool(getattr(settings, 'DNS_APPROVAL_ENABLED', True))


def build_payload(record):
    """把（可能尚未入库的）记录对象转成变更单目标值。"""
    payload = {}
    for field in PAYLOAD_FIELDS:
        if field == 'server_id':
            payload[field] = getattr(record, 'server_id', None)
        else:
            payload[field] = getattr(record, field, None)
    return payload


def create_request(domain, record, action, user, reason, existing=None):
    """建一张待审批变更单。

    `record` 是变更的**目标状态**（新增/修改后的值，可未入库）；`existing` 是
    库里那条现有记录（改/删时用于取线上前值与关联）。
    """
    before, source = ({}, 'none')
    if existing is not None:
        before, source = dns_history.remote_snapshot(existing)
    ttl_hours = getattr(settings, 'DNS_APPROVAL_TTL_HOURS', 24)
    return DnsChangeRequest.objects.create(
        domain=domain,
        record=existing,
        action=action,
        payload=build_payload(record),
        before=before,
        before_source=source,
        risk_reasons=dns_risk.risk_reasons(record, action, domain=domain),
        reason=(reason or '')[:200],
        requested_by=user if getattr(user, 'is_authenticated', False) else None,
        expires_at=timezone.now() + timedelta(hours=ttl_hours),
    )


def can_review(change_request, user, emergency=False):
    """审批资格检查，返回错误消息或 None。

    申请人不能自批——除非显式走紧急通道。10 人团队里最容易被绕过的就是这条，
    所以判定放在这里而不是靠页面隐藏按钮。
    """
    if not change_request.pending:
        if change_request.expired:
            return '变更单已过期，请重新提交。'
        return f'变更单当前状态为「{change_request.get_status_display()}」，不可审批。'
    if not emergency and change_request.requested_by_id == getattr(user, 'pk', None):
        return ('不能审批自己提交的变更单。确需自行执行请走紧急通道'
                '（emergency=true 并填写原因），该操作会高亮记入审计。')
    return None


def apply_request(change_request, user, emergency=False, comment=''):
    """审批通过并执行，返回 (成功?, 消息)。

    执行顺序：资格 → 过期 → 漂移比对 → 云端 → 本地 → 留痕 → 变更单终态。
    云端失败整体放弃，变更单保持 pending 供修正后重试。
    """
    error = can_review(change_request, user, emergency=emergency)
    if error:
        if change_request.expired:
            _mark_expired(change_request)
        return False, error
    if emergency and not (comment or '').strip():
        return False, '紧急自批必须填写原因（会高亮记入审计）。'

    drift = _drift_message(change_request)
    if drift:
        return False, drift

    try:
        record, log = _execute(change_request, user)
    except DnsProviderError as exc:
        return False, f'厂商侧执行失败，变更单保持待审批：{exc}'
    except Exception as exc:  # noqa: BLE001 - 兜住 SDK/网络层异常，别把 500 抛给页面
        logger.exception('DNS 变更单执行失败：#%s', change_request.pk)
        return False, f'执行失败，变更单保持待审批：{exc}'

    change_request.record = record
    change_request.status = DnsChangeRequest.STATUS_APPLIED
    change_request.reviewed_by = user if getattr(user, 'is_authenticated', False) else None
    change_request.reviewed_at = timezone.now()
    change_request.review_comment = (comment or '')[:200]
    change_request.emergency = emergency
    change_request.change_log = log
    change_request.save()
    level = logger.warning if emergency else logger.info
    level('DNS 变更单 #%s 已执行（%s %s.%s）%s',
          change_request.pk, change_request.action, change_request.target_rr,
          change_request.domain.name, '【紧急自批】' if emergency else '')
    return True, ('已执行（紧急自批，已记入审计）' if emergency else '已审批并执行')


def reject(change_request, user, comment=''):
    if not change_request.pending:
        if change_request.expired:
            _mark_expired(change_request)
        return False, f'变更单当前状态为「{change_request.get_status_display()}」，不可驳回。'
    change_request.status = DnsChangeRequest.STATUS_REJECTED
    change_request.reviewed_by = user if getattr(user, 'is_authenticated', False) else None
    change_request.reviewed_at = timezone.now()
    change_request.review_comment = (comment or '')[:200]
    change_request.save()
    return True, '已驳回'


def cancel(change_request, user):
    """申请人撤销自己的变更单（管理员也可撤）。"""
    if not change_request.pending:
        return False, f'变更单当前状态为「{change_request.get_status_display()}」，不可撤销。'
    is_owner = change_request.requested_by_id == getattr(user, 'pk', None)
    if not (is_owner or getattr(user, 'is_staff', False)):
        return False, '只能撤销自己提交的变更单。'
    change_request.status = DnsChangeRequest.STATUS_CANCELLED
    change_request.save(update_fields=['status', 'updated_at'])
    return True, '已撤销'


def expire_stale():
    """把过期的待审批变更单标为已过期，返回条数（供定时任务调用）。"""
    stale = DnsChangeRequest.objects.filter(
        status=DnsChangeRequest.STATUS_PENDING, expires_at__lte=timezone.now())
    return stale.update(status=DnsChangeRequest.STATUS_EXPIRED,
                        updated_at=timezone.now())


# ── 内部 ──────────────────────────────────────────────────
def _mark_expired(change_request):
    change_request.status = DnsChangeRequest.STATUS_EXPIRED
    change_request.save(update_fields=['status', 'updated_at'])


def _drift_message(change_request):
    """线上值是否已与申请时不一致；不一致返回可读的拒绝消息。"""
    if change_request.action == 'create' or change_request.record is None:
        return None
    if change_request.before_source != 'provider':
        return None  # 申请时就没拿到可信前值，无从比对
    current, source = dns_history.remote_snapshot(change_request.record)
    if source != 'provider':
        return '无法读取当前线上值，暂不执行（避免把陈旧目标值推上线），请稍后重试。'
    changed = [
        field for field in ('rr', 'type', 'value', 'ttl', 'line', 'enabled')
        if str(current.get(field)) != str(change_request.before.get(field))
    ]
    if not changed:
        return None
    diff = '、'.join(f'{f}: {change_request.before.get(f)} → {current.get(f)}'
                    for f in changed)
    return (f'线上值在申请后已被改动（{diff}），本变更单已失去基准，'
            f'请撤销并重新提交。')


def _execute(change_request, user):
    """按变更单动作执行，返回 (记录对象或 None, 变更留痕)。"""
    domain = change_request.domain
    provider = get_dns_provider(domain.cloud_account)
    if change_request.action == 'delete':
        record = change_request.record
        if record is None:
            raise DnsProviderError('目标记录已不存在（可能已被删除）。')
        before, source = dns_history.remote_snapshot(record)
        if record.remote_record_id:
            provider.delete_record(domain.name, record.remote_record_id)
        record.delete()
        log = dns_history.log_change(
            record, 'delete', user, before=before, before_source=source,
            reason=f'变更单 #{change_request.pk}：{change_request.reason}', domain=domain)
        return None, log

    record = change_request.record or DnsRecord(domain=domain)
    before, source = (({}, 'none') if change_request.record is None
                      else dns_history.remote_snapshot(record))
    _apply_payload(record, change_request.payload)
    if record.remote_record_id:
        provider.update_record(domain.name, dns_history.to_provider_data(record))
    else:
        record.remote_record_id = provider.add_record(
            domain.name, dns_history.to_provider_data(record))
    record.sync_status, record.sync_error = 'synced', ''
    record.save()
    log = dns_history.log_change(
        record, change_request.action, user, before=before, before_source=source,
        after=dns_history.local_snapshot(record),
        reason=f'变更单 #{change_request.pk}：{change_request.reason}', domain=domain)
    return record, log


def _apply_payload(record, payload):
    for field in PAYLOAD_FIELDS:
        if field not in payload or payload[field] is None:
            continue
        setattr(record, field, payload[field])
