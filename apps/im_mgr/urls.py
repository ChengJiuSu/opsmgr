from django.urls import path, re_path
from rest_framework.routers import DefaultRouter

from .views import (
    FeishuAppCallbackSlugView,
    FeishuAppView,
    FeishuCardView,
    FeishuChannelViewSet,
    FeishuEventView,
    FeishuSsoAuthorizeView,
    FeishuSsoCallbackView,
    FeishuSsoStatusView,
)

router = DefaultRouter()
router.register('channels', FeishuChannelViewSet, basename='feishu-channel')

# 回调入口的 slug 用严格的 32 位十六进制匹配：不合形状的请求连视图都进不去，
# 省掉一次配置读取与一次恒定时间比对。真正的标识校验仍在视图里做。
SLUG = r'(?P<slug>[0-9a-f]{32})'

urlpatterns = [
    # 公网回调（免登录，走签名校验）。前缀 /api/feishu/callback/ 与配置面分开，
    # 审计中间件据此整段跳过回调流量（见 apps/audit/middleware.py）
    re_path(rf'^callback/{SLUG}/event/$', FeishuEventView.as_view(), name='feishu-event'),
    re_path(rf'^callback/{SLUG}/card/$', FeishuCardView.as_view(), name='feishu-card'),

    # 飞书登录（免登录入口）。授权码由浏览器带回，飞书服务器不访问这些地址，
    # 因此不需要像 /callback/ 那样额外开放与加固
    path('sso/status/', FeishuSsoStatusView.as_view(), name='feishu-sso-status'),
    path('sso/authorize/', FeishuSsoAuthorizeView.as_view(), name='feishu-sso-authorize'),
    path('sso/callback/', FeishuSsoCallbackView.as_view(), name='feishu-sso-callback'),

    path('app/', FeishuAppView.as_view(), name='feishu-app'),
    path('app/rotate-slug/', FeishuAppCallbackSlugView.as_view(), name='feishu-app-rotate-slug'),
] + router.urls
