"""制品上报接口的机读凭证校验（**已废弃**，废弃期兜底用）。

新调用方一律用 API 凭证（apps/auth_mgr/apikey.py）：按凭证独立 key、带 IP
白名单与作用域、可轮换可停用可审计。这里的全局单 key 只为存量 Jenkins 任务
平滑过渡而保留，命中即打 warning，据此盘点还有谁没换。存量清零后删除本文件、
`artifact.api_key` 系统配置项与视图上的兜底权限类。
"""
import hmac
import logging

from rest_framework.permissions import BasePermission

from .models import _api_key

logger = logging.getLogger('apps.artifact_mgr')


class HasArtifactAPIKey(BasePermission):
    """校验 X-API-KEY 请求头与系统配置 artifact.api_key（环境变量兜底）一致。

    key 未配置时一律拒绝——空 key 放行等于接口全开。
    """

    message = ('API Key 校验失败。新调用方请使用后台「API 凭证」页签发的 key'
               '（形如 ak_xxx.yyy），旧的全局 artifact.api_key 即将下线。')

    def has_permission(self, request, view):
        key = _api_key()
        provided = request.headers.get('X-API-KEY', '')
        matched = bool(key) and hmac.compare_digest(provided, key)
        if matched:
            logger.warning('制品上报仍在使用已废弃的全局 artifact.api_key，'
                           '请改用后台签发的 API 凭证（带 IP 白名单与作用域）')
        return matched
