from django.apps import AppConfig


class ImMgrConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.im_mgr'
    verbose_name = '即时通讯接入'

    def ready(self):
        """订阅任务执行的状态广播，用于刷新飞书卡片。

        订阅关系建在这一侧：task_exec 不该知道 im_mgr 的存在，否则「装不装飞书」
        就成了执行链路的依赖，而飞书发不出去时任务必须照跑。
        """
        from apps.task_exec.signals import execution_updated

        from . import notify

        execution_updated.connect(notify.on_execution_updated,
                                  dispatch_uid='im_mgr.card_refresh')
