from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    PortalBlockViewSet,
    PortalConfigView,
    PortalNavGroupViewSet,
    PortalNavItemViewSet,
    PortalNoticeViewSet,
    PortalSettingView,
    PortalSystemViewSet,
)

router = DefaultRouter()
router.register('blocks', PortalBlockViewSet, basename='portal-block')
router.register('systems', PortalSystemViewSet, basename='portal-system')
router.register('nav-groups', PortalNavGroupViewSet, basename='portal-nav-group')
router.register('nav-items', PortalNavItemViewSet, basename='portal-nav-item')
router.register('notices', PortalNoticeViewSet, basename='portal-notice')

urlpatterns = [
    # 聚合读接口放在 router 之前：避免被 router 的通配路由吃掉
    path('config/', PortalConfigView.as_view(), name='portal-config'),
    path('setting/', PortalSettingView.as_view(), name='portal-setting'),
    path('', include(router.urls)),
]
