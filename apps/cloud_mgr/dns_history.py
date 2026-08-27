"""解析记录的变更留痕与回滚。

拆成独立模块而不是塞进 views：留痕要在「云端调用之前」取值、在「之后」落库，
和视图的 CRUD 流程交织，混在一起读起来分不清哪段是业务哪段是留痕。

设计要点：

- **前值从厂商侧拉**，不用本地值。`sync_status=failed` 的记录本地与云端本就
  不一致，拉云端才是真实前值；拉不到时退回本地并标注 `before_source`，否则
  回滚时分不清这份快照可不可信（只有 provider 来源的才允许回滚）。
- **留痕失败不阻断业务**：DNS 已经改完了，这时再抛异常只会让调用方以为没改成功
  然后重试一次。只告警。
- 回滚自身也落一条 `action='restore'` 的留痕，否则「谁把它改回去的」又查不到。
"""
import logging

from django.utils import timezone

from .models import DnsChangeLog, DnsRecord
from .providers import DnsProviderError, DnsRecordData, get_dns_provider

logger = logging.getLogger(__name__)

#: 快照字段：够重建一条记录，也够比对差异
SNAPSHOT_FIELDS = ('rr', 'type', 'value', 'ttl', 'line', 'enabled', 'remote_record_id')


def to_provider_data(record):
    """DnsRecord → 厂商入参（与 views 同口径）。"""
    return DnsRecordData(
        rr=record.rr, type=record.type, value=record.value,
        ttl=record.ttl, line=record.line or 'default',
        remote_record_id=record.remote_record_id,
        enabled=record.enabled,
    )


def local_snapshot(record):
    """本地当前值快照。"""
    return {field: getattr(record, field) for field in SNAPSHOT_FIELDS}


def remote_snapshot(record):
    """厂商侧当前值快照，返回 (快照, 来源)。

    provider 没有「按 id 取单条」的接口，只能拉列表再按 remote_record_id 找。
    DNS 变更本就低频，多一次列表调用换一份可信前值是划算的。
    """
    if not record.remote_record_id:
        return local_snapshot(record), 'local'
    try:
        provider = get_dns_provider(record.domain.cloud_account)
        for item in provider.list_records(record.domain.name):
            if str(item.remote_record_id) == str(record.remote_record_id):
                return {
                    'rr': item.rr, 'type': item.type, 'value': item.value,
                    'ttl': item.ttl, 'line': item.line or 'default',
                    'enabled': getattr(item, 'enabled', True),
                    'remote_record_id': str(item.remote_record_id),
                }, 'provider'
        # 云端已经没有这条了（别处删过）：记下这个事实，它本身就是排障线索
        return local_snapshot(record), 'missing'
    except Exception as exc:  # noqa: BLE001 - 见下
        # 刻意宽泛：这是「取一份更可信的前值」的尽力而为，厂商 SDK/网络层能抛出
        # 的异常类型不止 DnsProviderError。为了一份快照把用户的 DNS 修改整个打断
        # 是本末倒置——退回本地值并标注来源，回滚侧只信 provider 来源的快照。
        logger.warning('拉取解析记录云端前值失败，退回本地值：%s（%s）',
                       record.remote_record_id, exc)
        return local_snapshot(record), 'local'


def log_change(record, action, user, before=None, before_source='none',
               after=None, reason='', domain=None):
    """落一条变更留痕；失败只告警，绝不阻断已经完成的 DNS 变更。"""
    domain = domain or getattr(record, 'domain', None)
    try:
        return DnsChangeLog.objects.create(
            domain=domain,
            domain_name=getattr(domain, 'name', '') or '',
            record=record if record and record.pk else None,
            record_rr=getattr(record, 'rr', '') or '',
            record_type=getattr(record, 'type', '') or '',
            remote_record_id=(before or {}).get('remote_record_id')
            or getattr(record, 'remote_record_id', '') or '',
            action=action,
            before=before or {},
            before_source=before_source,
            after=after or {},
            reason=(reason or '')[:200],
            operator=user if getattr(user, 'is_authenticated', False) else None,
        )
    except Exception:  # noqa: BLE001 - 留痕失败不能让调用方以为 DNS 没改成功
        logger.exception('DNS 变更留痕写入失败：%s %s', action, getattr(record, 'rr', ''))
        return None


def restore(change, user):
    """按留痕回滚，返回 (成功?, 消息)。

    三种动作对应三种反向操作：
    - create → 删掉这条记录（撤销误加）；
    - update → 把前值写回；
    - delete → 按前值重建（记录本地行已经没了，要新建）。

    云端先动、本地后动：云端失败就整体放弃，避免出现「本地显示已回滚、线上还是
    错值」——那比不回滚更危险，因为看页面的人会以为已经好了。
    """
    if not change.restorable:
        return False, '该变更不可回滚（已回滚过 / 无可信前值 / 记录已不存在）。'
    if change.domain is None:
        return False, '主域名已被删除，无法回滚（需先重新导入域名）。'

    handlers = {
        'create': _undo_create,
        'update': _restore_value,
        'delete': _recreate,
    }
    try:
        ok_flag, message, record = handlers[change.action](change)
    except DnsProviderError as exc:
        return False, f'厂商侧回滚失败，未做任何改动：{exc}'
    except Exception as exc:  # noqa: BLE001 - 兜住 SDK/网络层异常，别把 500 抛给页面
        logger.exception('DNS 回滚失败：#%s', change.pk)
        return False, f'回滚失败，未做任何改动：{exc}'
    if not ok_flag:
        return False, message

    change.restored_at = timezone.now()
    change.restored_by = user if getattr(user, 'is_authenticated', False) else None
    change.save(update_fields=['restored_at', 'restored_by'])
    log_change(
        record, 'restore', user,
        before=change.after, before_source=change.before_source,
        after=change.before, reason=f'回滚变更 #{change.pk}',
        domain=change.domain,
    )
    return True, message


def _undo_create(change):
    """撤销新增：删云端再删本地。"""
    record = change.record
    if record is None:
        return False, '记录已不存在，无需撤销。', None
    provider = get_dns_provider(change.domain.cloud_account)
    if record.remote_record_id:
        provider.delete_record(change.domain.name, record.remote_record_id)
    rr = record.rr
    record.delete()
    # delete() 把内存实例的 pk 置空，而 change.record 还指着它——不清掉的话
    # 接下来 change.save() 会被 Django 拦下（unsaved related object）。
    # 库里的外键已由 SET_NULL 处理，这里只是同步内存状态。
    change.record = None
    return True, f'已撤销新增：{rr}.{change.domain.name} 已删除。', None


def _restore_value(change):
    """改回前值：记录还在就更新，记录后来被删了就重建。"""
    record = change.record
    if record is None:
        return _recreate(change)
    provider = get_dns_provider(change.domain.cloud_account)
    _apply(record, change.before)
    provider.update_record(change.domain.name, to_provider_data(record))
    record.sync_status, record.sync_error = 'synced', ''
    record.save()
    return True, f'已回滚到变更前值：{record.rr}.{change.domain.name} → {record.value}', record


def _recreate(change):
    """按前值重建被删的记录（云端新增会拿到新的 remote_record_id）。"""
    snapshot = change.before
    record = DnsRecord(domain=change.domain, target_type='none')
    _apply(record, snapshot)
    record.remote_record_id = ''
    provider = get_dns_provider(change.domain.cloud_account)
    record.remote_record_id = provider.add_record(change.domain.name, to_provider_data(record))
    record.sync_status, record.sync_error = 'synced', ''
    record.save()
    return True, f'已按前值重建：{record.rr}.{change.domain.name} → {record.value}', record


def _apply(record, snapshot):
    """把快照写进记录对象（remote_record_id 由调用方决定，不在这里覆盖）。"""
    for field in SNAPSHOT_FIELDS:
        if field == 'remote_record_id':
            continue
        if field in snapshot:
            setattr(record, field, snapshot[field])
