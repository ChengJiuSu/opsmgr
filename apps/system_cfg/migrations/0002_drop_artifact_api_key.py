"""删除已下线的 artifact.api_key 系统配置项。

它是一把明文存在库里的秘密，功能上已经没有任何读取方（上报走 API 凭证、下载签名
走 ARTIFACT_SIGN_KEY），留着只是把一个还能用的旧凭据继续摊在配置面上给人看。

**reverse 是 noop**：迁移不保存被删的值（保存等于把这个秘密再抄一份进迁移文件）。
真要回退到旧口径，得从备份里取回原值再手工写回——而那也意味着要把兜底代码一起
回退，不是这条迁移能单独完成的事。
"""
from django.db import migrations

LEGACY_KEY = 'artifact.api_key'


def forwards(apps, schema_editor):
    SystemConfig = apps.get_model('system_cfg', 'SystemConfig')
    SystemConfig.objects.filter(key=LEGACY_KEY).delete()


class Migration(migrations.Migration):
    dependencies = [('system_cfg', '0001_initial')]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
