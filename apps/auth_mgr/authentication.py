"""带 MFA 强制的 JWT 认证类（全局 DEFAULT_AUTHENTICATION_CLASSES）。

强制检查放在**认证类**而不是权限类里：项目大量 ViewSet 自己声明了
`permission_classes = [IsAdmin]` 等，全局默认权限对它们不生效，放权限层等于
漏掉一半接口。认证类是全局的，只有那些自带认证方式的机器接口（制品上报的
X-API-KEY）会绕过——那正是应该绕过的。

判定只读 token 里的 `amr` 声明，不查库：`amr` 含 `otp` 说明这条会话过了第二
因子；只有 `pwd` 且该用户在强制范围内，就挡住并让前端跳去绑定页。
"""
from rest_framework.exceptions import PermissionDenied
from rest_framework_simplejwt.authentication import JWTAuthentication

from . import services

# 未完成绑定时仍可访问的路径：绑定本身、看自己是谁、改密码、登出、换 token。
# 少一条就会出现「被要求绑定，却连绑定接口都调不了」的死锁。
EXEMPT_PREFIXES = (
    '/api/auth/mfa/',
    '/api/auth/me/',
    '/api/auth/logout/',
    '/api/auth/change-password/',
    '/api/auth/token/',
    '/api/auth/login/',
)


class MfaEnrollmentRequired(PermissionDenied):
    """已登录但未完成 MFA 绑定（403 + error_code，前端据此跳绑定页）。"""

    default_detail = '按安全策略需先绑定二次认证（MFA）才能访问。'
    default_code = 'mfa_enrollment_required'


class MfaJWTAuthentication(JWTAuthentication):
    def authenticate(self, request):
        result = super().authenticate(request)
        if result is None:
            return None
        user, token = result
        if self._exempt(request) or self._passed_mfa(token):
            return result
        if services.required_for(user):
            raise MfaEnrollmentRequired()
        return result

    @staticmethod
    def _exempt(request):
        return str(request.path).startswith(EXEMPT_PREFIXES)

    @staticmethod
    def _passed_mfa(token):
        # 存量 token 没有 amr 声明：视为只过了密码。开启强制后这些会话会被挡，
        # 重新登录即可——这正是「开启强制」应有的效果。
        # otp（本平台 TOTP）与 sso（飞书登录）同档，理由见 services.AMR_STRONG。
        payload = getattr(token, 'payload', None) or {}
        claimed = set(payload.get('amr') or [])
        return bool(claimed & set(services.AMR_STRONG))
