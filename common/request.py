"""请求元信息提取（审计、限速共用，避免各处自己解析头）。"""


def client_ip(request):
    """客户端 IP：优先 X-Forwarded-For 首段（nginx 前置），回退 REMOTE_ADDR。

    XFF 由代理追加，链路形如 `真实客户端, 代理1, 代理2`，取第一段。
    注意：该头可被客户端伪造，只用于审计展示与限速分桶，不作鉴权依据。
    """
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '') or ''
