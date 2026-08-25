"""认证入口的失败计数限速（登录，后续 MFA 校验共用）。

用法：

    remaining = blocked_seconds('login_user', username)   # >0 = 仍在锁定期
    record_failure('login_user', username, limit=10, window=900, block=900)
    reset('login_user', username)                          # 成功后清零

语义：滑动窗口用「计数键 + TTL」近似——首次失败 `cache.add` 建键并带上窗口
TTL，之后 `incr`；键过期即计数归零。达到阈值时另写一个 block 键，值为解锁
时间戳，锁定期内 `blocked_seconds` 返回剩余秒数供 429 提示。

标识（用户名 / IP）先哈希再入键：既规避缓存后端对键字符的限制（空格、控制
字符），也不把用户名明文摊在 Redis 里。

**缓存不可用时放行（fail-open）并告警**：Redis 挂掉时把所有人挡在控制台外
比放宽限速更糟——运维平台正是故障时最需要能登进去的系统。限速是防爆破的
辅助手段，密码强度与 MFA 才是主防线。
"""
import hashlib
import logging
import time

from django.core.cache import cache

logger = logging.getLogger(__name__)

KEY_PREFIX = 'rl'


def _key(scope, ident, suffix=''):
    digest = hashlib.sha256(str(ident).encode('utf-8')).hexdigest()[:32]
    return f'{KEY_PREFIX}:{scope}:{digest}{suffix}'


def blocked_seconds(scope, ident):
    """仍需等待的秒数；0 = 未锁定（缓存异常也返回 0，见模块说明）。"""
    unlock_at = _safe(lambda: cache.get(_key(scope, ident, ':block')), None)
    if not unlock_at:
        return 0
    remaining = int(unlock_at - time.time())
    return remaining if remaining > 0 else 0


def record_failure(scope, ident, limit, window, block):
    """记一次失败；达到阈值则锁定，返回锁定剩余秒数（0 = 未锁定）。"""
    count = _safe(lambda: _incr(_key(scope, ident), window), 0)
    if count < limit:
        return 0
    unlock_at = time.time() + block
    _safe(lambda: cache.set(_key(scope, ident, ':block'), unlock_at, timeout=block))
    # 锁定后清计数，解锁时从零开始，避免锁一次就永远差一次失败又被锁
    _safe(lambda: cache.delete(_key(scope, ident)))
    return block


def reset(scope, ident):
    """认证成功后清零计数与锁定。"""
    _safe(lambda: cache.delete_many([_key(scope, ident), _key(scope, ident, ':block')]))


def _incr(key, window):
    """窗口内累计失败次数。首次 add 带 TTL，之后 incr 不续期（固定窗口）。"""
    if cache.add(key, 1, timeout=window):
        return 1
    try:
        return cache.incr(key)
    except ValueError:
        # add 与 incr 之间键正好过期：按新窗口的第一次算
        cache.set(key, 1, timeout=window)
        return 1


def _safe(func, default=None):
    """缓存异常不影响主流程（fail-open），只告警。"""
    try:
        return func()
    except Exception:  # noqa: BLE001 - 缓存后端异常类型由实现决定
        logger.warning('限速缓存不可用，本次放行', exc_info=True)
        return default
