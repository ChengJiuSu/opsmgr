"""时间输出统一口径：手拼 dict 的接口必须过 local_dt。

DRF 的两条路径对 datetime 的处理不一样，这是这个模块存在的唯一原因：

- 走序列化器字段（``serializers.DateTimeField``）：内部会 ``enforce_timezone``，
  按 ``settings.TIME_ZONE`` 输出 ``2026-09-09T10:09:06+08:00``。
- 手拼 dict 直接塞 ``Response``：由 ``rest_framework.utils.encoders.JSONEncoder``
  兜底，它只调 ``isoformat()`` 不做时区转换，datetime 从库里取出来是 UTC，
  于是输出 ``2026-09-09T02:09:06Z``——比前者早 8 小时。

前端各页普遍是 ``t.slice(0, 19).replace('T', ' ')`` 直接截字符串显示，截到什么
就显示什么，所以第二条路径会在页面上实打实少 8 小时（概览页「各环境当前版本」
的上报时间就是这么错的）。凡是不经序列化器往响应里放 datetime 的地方，都包一层
``local_dt``。
"""

from django.utils import timezone


def local_dt(value):
    """aware datetime → 本地时区（TIME_ZONE）的 datetime；None 原样返回。

    返回 datetime 而不是字符串：调用方可能还要拿它算差值，格式化仍交给
    JSONEncoder，保持与序列化器字段一致的 ISO 输出。
    """
    return timezone.localtime(value) if value else None
