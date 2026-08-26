"""敏感操作二次验证（sudo 模式）。

登录时验过一次 MFA 之后，访问令牌能用 60 分钟、refresh 能用 7 天。对「改凭据、
建用户、发 Agent token、改系统配置」这类**持久扩大权限**的操作，这个跨度太长
了：一条被偷走的 token 在窗口内可以悄悄把整套凭据换掉。所以这些接口额外要求
一次「刚刚证明过是本人」——即 sudo 窗口。

窗口状态放缓存，键绑定**当前访问令牌的 jti**：

    sudo:<user_id>:<jti>  →  1（TTL = settings.SUDO_TTL）

绑 jti 而不是只绑用户：同一个人的另一条会话（或被偷走的另一个 token）拿不到
这次授权。代价是静默刷新令牌后窗口失效，需要再验一次——access 寿命 60 分钟、
sudo 只 10 分钟，实际很少撞上。

**这里 fail-closed**：缓存不可用时视为无授权、拒绝敏感操作。与 common.ratelimit
的 fail-open 刻意相反——限速挂了放行只是少一层防爆破，sudo 挂了放行等于这道
门直接没了；而拒绝的后果仅限于「改不了配置」，日常读写照常。
"""
import logging

from django.conf import settings
from django.core.cache import cache
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import SAFE_METHODS, BasePermission

logger = logging.getLogger(__name__)

KEY_PREFIX = 'sudo:'


class SudoRequiredError(PermissionDenied):
    """需要二次验证（403 + error_code，前端据此弹验证码框并重放请求）。"""

    default_detail = '该操作需要二次验证，请输入验证码后重试。'
    default_code = 'sudo_required'


def _key(user_id, jti):
    return f'{KEY_PREFIX}{user_id}:{jti}'


def token_jti(request):
    """当前访问令牌的 jti；非 JWT 认证（如 X-API-KEY 机器接口）返回 None。"""
    payload = getattr(getattr(request, 'auth', None), 'payload', None) or {}
    return payload.get('jti')


def grant(user_id, jti, ttl=None):
    """开一个 sudo 窗口，返回有效秒数（缓存不可用抛异常由调用方处理）。"""
    ttl = ttl or settings.SUDO_TTL
    cache.set(_key(user_id, jti), 1, timeout=ttl)
    return ttl


def is_active(user_id, jti):
    if not jti:
        return False
    try:
        return bool(cache.get(_key(user_id, jti)))
    except Exception:  # noqa: BLE001 - 缓存后端异常类型由实现决定
        # fail-closed：宁可让人多验一次，也不能在缓存故障时把门敞开
        logger.warning('sudo 缓存不可用，按无授权处理', exc_info=True)
        return False


def revoke(user_id, jti):
    """主动结束窗口（改密码、解绑 MFA 等身份要素变更后调用）。"""
    if not jti:
        return
    try:
        cache.delete(_key(user_id, jti))
    except Exception:  # noqa: BLE001
        logger.warning('sudo 窗口清理失败', exc_info=True)


class SudoRequired(BasePermission):
    """写操作要求 sudo 窗口；读操作放行。

    与既有权限类**并列**使用（DRF 是 AND 语义），不替换：

        permission_classes = [IsAdmin, SudoRequired]
    """

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        user = getattr(request, 'user', None)
        if not (user and user.is_authenticated):
            return True  # 认证与否交给 IsAuthenticated，避免把 401 说成 403
        if is_active(user.pk, token_jti(request)):
            return True
        raise SudoRequiredError()
