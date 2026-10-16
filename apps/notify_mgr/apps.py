from django.apps import AppConfig


class NotifyMgrConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.notify_mgr'
    verbose_name = '消息通知'

    def ready(self):
        """接入各通知来源：注册事件类型并订阅业务方的信号。

        订阅关系建在通知这一侧：业务模块不该知道通知中心的存在，否则「通知发不
        出去」就成了业务链路的依赖。以后接新来源，在 sources/ 下加一个模块并在
        这里连上即可。
        """
        from apps.task_exec.signals import execution_updated

        from .sources import task_exec

        execution_updated.connect(task_exec.on_execution_updated,
                                  dispatch_uid='notify_mgr.task_exec')
