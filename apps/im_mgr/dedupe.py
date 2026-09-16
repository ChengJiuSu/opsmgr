"""一次性消费（nonce / event_id / 卡片确认 token 共用）。

飞书这条链路上有三处「同一件事可能到达两次」，且后果都是重复执行一次任务：

- **回调 nonce** —— 攻击者原样重放整个 HTTP 请求；
- **事件 event_id** —— 飞书自己的投递重试（同一事件、不同 nonce 与签名，所以
  nonce 判重挡不住它）；
- **卡片确认 token** —— 手抖双击，或卡片被翻旧账再点一次。

三处共用这一个原子的「首次消费」判定，避免各写一份 get + set——后者在并发下
两个请求会同时读到「不存在」然后都放行，而这正是要防的场景。

**fail-closed**：缓存不可用时抛 `DedupeUnavailable`，调用方一律拒绝本次请求。
与 common/sudo.py 同一取舍——放行的后果是可被重放触发生产执行，拒绝的后果只是
飞书那边报个失败、重试一次。
"""
import logging

from django.core.cache import cache

logger = logging.getLogger(__name__)

KEY_PREFIX = 'feishu:once:'


class DedupeUnavailable(Exception):
    """判重缓存不可用（调用方按「已重复」处理）。"""


def consume(namespace, value, ttl):
    """消费一次；返回 True 表示首次出现，False 表示窗口内已见过。"""
    if not value:
        return False
    key = f'{KEY_PREFIX}{namespace}:{value}'
    try:
        # cache.add = set-if-absent，原子
        return bool(cache.add(key, 1, timeout=ttl))
    except Exception as exc:  # noqa: BLE001 - 缓存后端异常类型由实现决定
        logger.warning('判重缓存不可用（%s）', namespace, exc_info=True)
        raise DedupeUnavailable(str(exc)) from exc
