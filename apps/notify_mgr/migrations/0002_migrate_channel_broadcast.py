"""把飞书会话上的播报配置（FeishuChannel.notify_*）搬成订阅规则。

语义原样保留：`notify_scope='failed'` 的会话只订阅失败类事件（失败 + 最终失败），
`'all'` 的再加上成功；项目组 / 环境的单值订阅变成规则上的多选。

幂等：按（规则名, 目标会话）get_or_create，重跑不重复建。
可回滚：回滚时把规则写回会话字段（多选只能取第一个值——旧字段是单值）。
"""
from django.db import migrations

FAILED_EVENTS = ['task_exec.failed', 'task_exec.failure_closed']
SUCCESS_EVENT = 'task_exec.success'


def forwards(apps, schema_editor):
    FeishuChannel = apps.get_model('im_mgr', 'FeishuChannel')
    NotifyRule = apps.get_model('notify_mgr', 'NotifyRule')

    for chat in FeishuChannel.objects.filter(notify_enabled=True):
        events = list(FAILED_EVENTS)
        if chat.notify_scope == 'all':
            events.insert(0, SUCCESS_EVENT)
        rule, _ = NotifyRule.objects.get_or_create(
            name=f'{chat.name} 播报'[:100], feishu_chat=chat,
            defaults={
                'enabled': chat.enabled,
                'events': events,
                'target_type': 'feishu_chat',
                'remark': '由飞书会话播报配置迁移',
            })
        if chat.notify_project_group_id:
            rule.project_groups.add(chat.notify_project_group_id)
        if chat.notify_environment_id:
            rule.environments.add(chat.notify_environment_id)


def backwards(apps, schema_editor):
    FeishuChannel = apps.get_model('im_mgr', 'FeishuChannel')
    NotifyRule = apps.get_model('notify_mgr', 'NotifyRule')

    FeishuChannel.objects.update(notify_enabled=False)
    for rule in NotifyRule.objects.filter(target_type='feishu_chat', feishu_chat__isnull=False):
        chat = rule.feishu_chat
        chat.notify_enabled = True
        chat.notify_scope = 'all' if SUCCESS_EVENT in (rule.events or []) else 'failed'
        group = rule.project_groups.first()
        env = rule.environments.first()
        chat.notify_project_group_id = group.pk if group else None
        chat.notify_environment_id = env.pk if env else None
        chat.save(update_fields=['notify_enabled', 'notify_scope',
                                 'notify_project_group', 'notify_environment'])


class Migration(migrations.Migration):

    dependencies = [
        ('notify_mgr', '0001_initial'),
        ('im_mgr', '0010_feishuapp_event_mode'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
