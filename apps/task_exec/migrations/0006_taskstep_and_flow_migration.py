"""任务流改造：新增 TaskStep 模块表，存量任务平移为单模块任务流，再删任务级内容字段。

操作顺序（同一迁移内）：CreateModel/AddField → RunPython 数据平移 → RemoveField。
"""
import django.db.models.deletion
from django.db import migrations, models


def forwards_copy_to_steps(apps, schema_editor):
    """存量任务的内容字段平移为任务流的第 1 个模块（幂等：已有模块则跳过）。"""
    ExecutionTask = apps.get_model('task_exec', 'ExecutionTask')
    TaskStep = apps.get_model('task_exec', 'TaskStep')
    type_names = {'command': '命令', 'playbook': 'Playbook', 'method': '平台方法'}
    for task in ExecutionTask.objects.all().iterator():
        if TaskStep.objects.filter(task=task).exists():
            continue
        TaskStep.objects.create(
            task=task,
            order=1,
            name=f'{type_names.get(task.task_type, task.task_type)}模块',
            task_type=task.task_type,
            command=task.command or '',
            playbook=task.playbook or '',
            method_id=task.method_id,
            tags=task.tags or '',
            extra_vars=task.extra_vars or '',
            timeout=task.timeout or 300,
        )


def backwards_noop(apps, schema_editor):
    """回滚仅恢复表结构，模块数据不回写任务字段（历史快照仍在日志）。"""


class Migration(migrations.Migration):

    dependencies = [
        ('asset_mgr', '0007_serverservice_process_count_and_more'),
        ('method_mgr', '0002_method_service_type'),
        ('task_exec', '0005_executiontask_channel_executiontask_environment_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='TaskStep',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('order', models.PositiveIntegerField(default=1, verbose_name='执行顺序')),
                ('name', models.CharField(max_length=100, verbose_name='模块名称')),
                ('task_type', models.CharField(choices=[('command', '命令'), ('playbook', 'Playbook'), ('method', '平台方法')], default='command', max_length=20, verbose_name='模块类型')),
                ('command', models.TextField(blank=True, verbose_name='Shell 命令')),
                ('playbook', models.CharField(blank=True, max_length=500, verbose_name='剧本路径')),
                ('tags', models.CharField(blank=True, max_length=255, verbose_name='Tags')),
                ('extra_vars', models.TextField(blank=True, verbose_name='额外变量')),
                ('timeout', models.PositiveIntegerField(default=300, verbose_name='超时(秒)')),
                ('use_custom_scope', models.BooleanField(default=False, verbose_name='自定义主机范围')),
                ('channel', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='task_steps', to='asset_mgr.channel', verbose_name='渠道')),
                ('environment', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='task_steps', to='asset_mgr.environment', verbose_name='环境')),
                ('method', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='steps', to='method_mgr.method', verbose_name='平台方法')),
                ('project_group', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='task_steps', to='asset_mgr.projectgroup', verbose_name='项目组')),
                ('task', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='steps', to='task_exec.executiontask', verbose_name='所属任务')),
            ],
            options={
                'verbose_name': '任务模块',
                'verbose_name_plural': '任务模块',
                'db_table': 'task_step',
                'ordering': ['order', 'id'],
            },
        ),
        migrations.AddField(
            model_name='taskexecutionlog',
            name='steps_result',
            field=models.JSONField(blank=True, default=list, verbose_name='模块执行结果'),
        ),
        # 数据平移必须在删字段之前执行
        migrations.RunPython(forwards_copy_to_steps, backwards_noop),
        migrations.RemoveField(model_name='executiontask', name='command'),
        migrations.RemoveField(model_name='executiontask', name='extra_vars'),
        migrations.RemoveField(model_name='executiontask', name='limit'),
        migrations.RemoveField(model_name='executiontask', name='method'),
        migrations.RemoveField(model_name='executiontask', name='params'),
        migrations.RemoveField(model_name='executiontask', name='pattern'),
        migrations.RemoveField(model_name='executiontask', name='playbook'),
        migrations.RemoveField(model_name='executiontask', name='tags'),
        migrations.RemoveField(model_name='executiontask', name='task_type'),
        migrations.RemoveField(model_name='executiontask', name='timeout'),
    ]
