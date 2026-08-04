"""方法执行器（provider）注册表与基类。

新增 provider 只需继承 BaseExecutor 并用 @register('<provider>') 装饰，
在 executors/__init__.py 中 import 一次即可被调度发现。
"""
from abc import ABC, abstractmethod

_REGISTRY = {}


def register(provider):
    def _decorator(cls):
        cls.provider = provider
        _REGISTRY[provider] = cls
        return cls

    return _decorator


def get_executor(provider):
    """返回 provider 对应的执行器实例，未注册则抛 KeyError。"""
    cls = _REGISTRY[provider]
    return cls()


def available_providers():
    return sorted(_REGISTRY.keys())


class BaseExecutor(ABC):
    """执行器基类。execute 返回 (return_code, output)。"""

    provider = None

    @abstractmethod
    def execute(self, task, method, inventory_path=None, servers=None, on_data=None):
        """执行方法，返回 (rc, output)。

        task 为 ExecutionTask，method 为 Method；inventory_path 为平台基于
        任务所选主机生成的动态 inventory 文件（task_exec.runner 传入）；
        servers 为模块匹配到的 Server 列表（不走 Ansible 的 provider，
        如 jumpserver 注册，直接消费主机对象）；on_data 为实时输出回调
        （边执行边把输出分片回传给任务日志，不支持流式的 provider 可忽略）。
        """
        raise NotImplementedError

    def validate_config(self, config):
        """校验方法配置，配置非法时抛 ValueError。默认不校验。"""
        return None
