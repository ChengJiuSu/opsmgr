"""请求元信息提取（审计、限速、IP 白名单共用，避免各处自己解析头）。

两个取 IP 的函数刻意分开，用途不能混：

- `client_ip`  —— 展示与限速分桶用。取 XFF 首段，**可被客户端伪造**。
- `auth_client_ip` —— 鉴权用（API 凭证 IP 白名单）。只信 settings.TRUSTED_PROXIES
  之内的转发，从 XFF 右端往左剥可信代理，取第一个非可信地址。

为什么必须分开：XFF 是普通请求头，客户端想写什么写什么。拿 `client_ip` 做
白名单，攻击者加一个 `X-Forwarded-For: 10.0.0.5` 就伪装成内网来源，白名单
等于没有。而审计侧要的恰恰是「客户端声称的链路原样记录」，两者需求相反。
"""
import ipaddress
import logging

from django.conf import settings

logger = logging.getLogger(__name__)


def client_ip(request):
    """客户端 IP：优先 X-Forwarded-For 首段（nginx 前置），回退 REMOTE_ADDR。

    XFF 由代理追加，链路形如 `真实客户端, 代理1, 代理2`，取第一段。
    注意：该头可被客户端伪造，只用于审计展示与限速分桶，不作鉴权依据。
    """
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '') or ''


def auth_client_ip(request):
    """鉴权可用的客户端 IP（IP 白名单唯一取值口径）。

    算法：
    1. `REMOTE_ADDR` 不在 TRUSTED_PROXIES 内 —— 说明请求没经过我们的代理，
       XFF 全部是客户端自己写的，整条丢掉，直接用 REMOTE_ADDR；
    2. 在可信代理内 —— 从 XFF **最右往左**依次剥掉连续的可信代理地址，
       返回第一个非可信地址。右端是离服务最近的一跳（由代理追加，客户端
       伪造不到），左端才是客户端可控区。

    nginx 侧必须是 `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`
    （追加，不是覆盖），多层代理要把每一层的地址都写进 TRUSTED_PROXIES，
    否则会把内层代理的 IP 当成客户端。

    剥完没有非可信地址时返回 REMOTE_ADDR（即代理自身地址）——它几乎不会在
    业务白名单里，于是校验失败。这是刻意的 fail-closed：配置不对时拒绝，
    而不是放过去。
    """
    remote = (request.META.get('REMOTE_ADDR', '') or '').strip()
    networks = trusted_proxy_networks()
    if not ip_in_networks(remote, networks):
        return remote
    chain = [part.strip() for part in
             request.META.get('HTTP_X_FORWARDED_FOR', '').split(',') if part.strip()]
    while chain:
        candidate = chain.pop()
        if not ip_in_networks(candidate, networks):
            return candidate
    return remote


def trusted_proxy_networks():
    """settings.TRUSTED_PROXIES 解析为网段列表；非法条目跳过并告警。"""
    networks = []
    for entry in getattr(settings, 'TRUSTED_PROXIES', None) or []:
        try:
            networks.append(ipaddress.ip_network(str(entry).strip(), strict=False))
        except ValueError:
            logger.warning('TRUSTED_PROXIES 条目非法，已忽略：%r', entry)
    return networks


def parse_ip_rules(text):
    """白名单文本 → 网段列表。

    支持逗号、空格、换行、分号混排；单个 IP 视为 /32（IPv6 /128）。
    任一条目非法直接抛 ValueError（消息含出错条目），供序列化器转中文报错——
    静默跳过非法条目会让「以为限制了，其实这条没生效」成为常态。
    """
    networks = []
    for entry in _split_rules(text):
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError as exc:
            raise ValueError(f'{entry}（{exc}）') from exc
    return networks


def ip_in_networks(ip, networks):
    """IP 是否命中任一网段；IP 或网段无法解析一律返回 False（fail-closed）。"""
    if not ip or not networks:
        return False
    try:
        addr = ipaddress.ip_address(str(ip).strip())
    except ValueError:
        return False
    return any(addr in net for net in networks)


def ip_matches_rules(ip, text):
    """IP 是否命中白名单文本；文本非法按不命中处理（fail-closed）。"""
    try:
        networks = parse_ip_rules(text)
    except ValueError:
        logger.warning('IP 白名单文本非法，按不匹配处理：%r', text)
        return False
    return ip_in_networks(ip, networks)


def _split_rules(text):
    normalized = str(text or '')
    for sep in (',', ';', '\n', '\r', '\t'):
        normalized = normalized.replace(sep, ' ')
    return [item for item in normalized.split(' ') if item]
