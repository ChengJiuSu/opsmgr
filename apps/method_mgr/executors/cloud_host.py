"""云主机管理方法执行器（预留）。

后续对接云平台 API（如创建/重启/销毁云主机）时在此实现。
当前作为扩展框架占位，返回未实现提示。
"""
from .base import BaseExecutor, register


@register('cloud_host')
class CloudHostExecutor(BaseExecutor):

    def execute(self, task, method, inventory_path=None, servers=None, on_data=None):
        action = (method.config or {}).get('action', '')
        return 1, f'云主机管理方法暂未实现（预留 provider）。config.action={action!r}'
