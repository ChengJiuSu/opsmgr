"""制品上报接口的机读凭证校验。"""
import hmac

from rest_framework.permissions import BasePermission

from .models import _api_key


class HasArtifactAPIKey(BasePermission):
    """校验 X-API-KEY 请求头与系统配置 artifact.api_key（环境变量兜底）一致。

    供 Jenkins 等 CI 脚本免登录上报制品；key 未配置时一律拒绝。
    """

    message = 'API Key 校验失败，请检查 X-API-KEY 请求头与系统配置 artifact.api_key。'

    def has_permission(self, request, view):
        key = _api_key()
        provided = request.headers.get('X-API-KEY', '')
        return bool(key) and hmac.compare_digest(provided, key)
