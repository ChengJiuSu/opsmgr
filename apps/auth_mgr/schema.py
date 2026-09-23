"""drf-spectacular 的认证扩展：让 schema 如实写出本项目的两种认证方式。

不注册的话 spectacular 对每个用到它们的视图报一条

    Warning [XxxViewSet]: could not resolve authenticator
    <class '...'>. There was no OpenApiAuthenticationExtension registered
    for that class. Ignoring for now.

「Ignoring for now」的后果是 schema 里这些接口的 security 是空的——生成的
文档、Swagger UI 的「Authorize」按钮、以及照 schema 生成的客户端，全都会把
整套需要登录的接口当成匿名可调。

本模块靠 import 触发注册（OpenApiAuthenticationExtension 用 __init_subclass__
登记），入口在 AuthMgrConfig.ready()，不要删那行 import。
"""
from drf_spectacular.contrib.rest_framework_simplejwt import SimpleJWTScheme
from drf_spectacular.extensions import OpenApiAuthenticationExtension

from .apikey import AUTH_KEYWORD, KEY_HEADER


class MfaJWTScheme(SimpleJWTScheme):
    """MfaJWTAuthentication 的 wire 格式与 simplejwt 完全一致。

    扩展是按 target_class 精确匹配的，不走 MRO——父类 JWTAuthentication 有
    官方扩展，子类却仍然「resolve 不到」，所以这里显式认领。
    MFA 强制只影响返回 403 与否，不改变请求怎么带凭据，因此直接复用官方定义。
    """

    target_class = 'apps.auth_mgr.authentication.MfaJWTAuthentication'
    name = 'jwtAuth'


class ApiKeyScheme(OpenApiAuthenticationExtension):
    """X-API-KEY：机读接口（制品上报 / Webhook）用的 API 凭证。"""

    target_class = 'apps.auth_mgr.apikey.ApiKeyAuthentication'
    name = 'apiKeyAuth'

    def get_security_definition(self, auto_schema):
        return {
            'type': 'apiKey',
            'in': 'header',
            'name': KEY_HEADER,
            'description': (
                f'API 凭证，格式 `ak_<key_id>.<secret>`。也可改用 '
                f'`Authorization: {AUTH_KEYWORD} <key>`。'
            ),
        }
