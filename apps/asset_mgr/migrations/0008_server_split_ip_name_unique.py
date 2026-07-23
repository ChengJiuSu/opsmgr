# 服务器 IP 拆分为公网/私网 + 主机名唯一：
#   - ip 改名 private_ip（RenameField 保留数据，历史 IP 均为私网地址）
#   - 新增 public_ip 可空，有值时唯一（部分唯一约束）
#   - name 加全局唯一约束（迁移前需保证现库无重名主机）
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('asset_mgr', '0007_serverservice_process_count_and_more'),
    ]

    operations = [
        migrations.RenameField(
            model_name='server',
            old_name='ip',
            new_name='private_ip',
        ),
        migrations.AlterField(
            model_name='server',
            name='private_ip',
            field=models.GenericIPAddressField(verbose_name='私网 IP'),
        ),
        migrations.AddField(
            model_name='server',
            name='public_ip',
            field=models.GenericIPAddressField(blank=True, null=True, verbose_name='公网 IP'),
        ),
        migrations.AlterField(
            model_name='server',
            name='name',
            field=models.CharField(max_length=100, unique=True, verbose_name='服务器名称'),
        ),
        migrations.AddConstraint(
            model_name='server',
            constraint=models.UniqueConstraint(
                condition=models.Q(('public_ip__isnull', False)),
                fields=('public_ip',),
                name='uniq_server_public_ip',
            ),
        ),
    ]
