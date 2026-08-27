"""给存量服务账号补 is_service 标记。

判据必须三条同时成立，缺一条都可能误标一个真人账号（`svc-` 前缀撞名、有人恰好
部门填成一样）：绑着 API 凭证、密码不可用、非 staff。

绑着凭证是最硬的一条——服务账号的存在意义就是被凭证引用。因此不靠用户名前缀判断。

可回滚：reverse 把标记清掉（字段本身由 0007 的 AlterField 反向删除）。
"""
from django.db import migrations


def forwards(apps, schema_editor):
    User = apps.get_model('auth_mgr', 'User')
    # 历史模型上没有 has_usable_password()，直接看 password 列：
    # set_unusable_password() 写入的是 '!' 开头的占位串
    User.objects.filter(
        api_credentials__isnull=False, is_staff=False, password__startswith='!',
    ).distinct().update(is_service=True)


def backwards(apps, schema_editor):
    User = apps.get_model('auth_mgr', 'User')
    User.objects.filter(is_service=True).update(is_service=False)


class Migration(migrations.Migration):
    dependencies = [('auth_mgr', '0007_user_is_service')]
    operations = [migrations.RunPython(forwards, backwards)]
