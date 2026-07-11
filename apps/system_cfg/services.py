"""系统配置读取服务。

供后端各模块（artifact_mgr 上报鉴权、method_mgr Jenkins 执行器等）按 key
读取系统配置表；配置由管理员在前端「系统配置」页维护。
"""
from .models import SystemConfig


def get_config(key, default=''):
    """按 key 读取配置值（去首尾空白），不存在时返回 default。"""
    value = (
        SystemConfig.objects.filter(key=key)
        .values_list('value', flat=True)
        .first()
    )
    if value is None:
        return default
    return value.strip()
