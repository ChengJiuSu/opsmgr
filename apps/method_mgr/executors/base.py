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
    def execute(self, task, method):
        """执行方法。task 为 ExecutionTask，method 为 Method。返回 (rc, output)。"""
        raise NotImplementedError

    def validate_config(self, config):
        """校验方法配置，配置非法时抛 ValueError。默认不校验。"""
        return None
