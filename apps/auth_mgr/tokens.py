"""MFA 挑战票据与访问令牌签发。

挑战票据（登录第一步返回、第二步换正式令牌）用独立的 `token_type='mfa'`：
simplejwt 的 `JWTAuthentication` 只认 `AUTH_TOKEN_CLASSES`（默认 AccessToken，
token_type='access'），类型不匹配会被拒——所以这张票据即便被当 Bearer 用也
拿不到任何接口，不需要额外拦截逻辑。

一次性靠缓存：用过的 jti 立刻拉黑（TTL = 票据剩余寿命），错误次数也按 jti
计数，超限直接作废票据、要求重新输密码。
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken, Token

from . import roles
from .services import AMR_OTP, AMR_PASSWORD, AMR_SSO

logger = logging.getLogger(__name__)

USED_PREFIX = 'mfa:used:'
ATTEMPT_PREFIX = 'mfa:attempt:'


class MfaChallengeToken(Token):
    """登录第一步（密码已过）签发的短时效票据。"""

    token_type = 'mfa'

    @property
    def lifetime(self):
        # 属性而非类属性：override_settings 改 TTL 时无需重载模块
        return timedelta(seconds=settings.MFA_CHALLENGE_TTL)


def issue_challenge(user):
    """签发挑战票据，返回 (token 字符串, 有效秒数)。"""
    token = MfaChallengeToken.for_user(user)
    return str(token), settings.MFA_CHALLENGE_TTL


def load_challenge(raw):
    """校验挑战票据，返回 MfaChallengeToken；无效/过期/已用过抛 TokenError。"""
    token = MfaChallengeToken(raw)  # 校验签名、exp、token_type
    jti = token.get('jti')
    if jti and cache.get(USED_PREFIX + jti):
        raise TokenError('该验证票据已使用，请重新登录。')
    return token


def consume_challenge(token):
    """作废票据（校验通过后调用），并清掉它的错误计数。"""
    jti = token.get('jti')
    if not jti:
        return
    _safe(lambda: cache.set(USED_PREFIX + jti, 1, timeout=settings.MFA_CHALLENGE_TTL))
    _safe(lambda: cache.delete(ATTEMPT_PREFIX + jti))


def count_attempt(token):
    """记一次验证码错误，返回剩余可试次数（0 = 票据已作废）。"""
    jti = token.get('jti')
    if not jti:
        return 0
    key = ATTEMPT_PREFIX + jti
    used = _safe(lambda: _incr(key), 1) or 1
    remaining = max(0, settings.MFA_MAX_ATTEMPTS - used)
    if remaining == 0:
        consume_challenge(token)  # 用完次数直接作废，逼回密码环节
    return remaining


def issue_access(user, amr=None):
    """签发正式令牌。

    `amr`（RFC 8176）写进 refresh，access 由 refresh 派生时自动继承，
    `/token/refresh/` 换新 access 也带着——强制策略靠它判断这条会话是否过了
    第二因子，不必每个请求查库。
    """
    refresh = RefreshToken.for_user(user)
    refresh['username'] = user.username
    refresh['department'] = user.department
    refresh['is_staff'] = user.is_staff
    # 与密码登录路径（CustomTokenObtainPairSerializer）保持同样的声明，
    # 否则走两步登录的会话前端拿不到权限快照，按钮全按最小权限渲染
    refresh['perms'] = roles.permission_snapshot(user)
    refresh['amr'] = list(amr or [AMR_PASSWORD])
    return {'refresh': str(refresh), 'access': str(refresh.access_token)}


def issue_after_mfa(user):
    return issue_access(user, amr=[AMR_PASSWORD, AMR_OTP])


def issue_after_sso(user):
    """外部身份源（飞书）登录后的令牌。

    amr 只写 `sso`，不带 `pwd`——用户压根没输密码，声明成输过是给审计与将来的
    策略判断埋错误信息。
    """
    return issue_access(user, amr=[AMR_SSO])


def _incr(key):
    if cache.add(key, 1, timeout=settings.MFA_CHALLENGE_TTL):
        return 1
    try:
        return cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=settings.MFA_CHALLENGE_TTL)
        return 1


def _safe(func, default=None):
    """缓存异常不阻断登录（与 common.ratelimit 同一取舍：fail-open + 告警）。

    注意由此带来的削弱：缓存不可用时票据一次性与错误计数都失效，只剩票据
    5 分钟寿命兜底。登录限速仍在，爆破空间有限。
    """
    try:
        return func()
    except Exception:  # noqa: BLE001 - 缓存后端异常类型由实现决定
        logger.warning('MFA 票据缓存不可用，本次放行', exc_info=True)
        return default
