"""导入各 provider 以完成注册，并对外暴露调度入口。"""
from .base import available_providers, get_executor  # noqa: F401

# 导入即触发 @register 注册；新增 provider 在此追加一行 import。
from . import ansible_playbook  # noqa: F401,E402
from . import cloud_host  # noqa: F401,E402
