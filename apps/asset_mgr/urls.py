from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    ChannelViewSet,
    CloudProviderViewSet,
    EnvironmentViewSet,
    ProjectGroupViewSet,
    ServerBatchDeleteView,
    ServerBatchUpdateView,
    ServerImportTemplateView,
    ServerImportView,
    ServerRegisterJumpServerView,
    ServerViewSet,
    ServiceTypeViewSet,
)

router = DefaultRouter()
router.register('servers', ServerViewSet, basename='server')
router.register('project-groups', ProjectGroupViewSet, basename='project-group')
router.register('channels', ChannelViewSet, basename='channel')
router.register('environments', EnvironmentViewSet, basename='environment')
router.register('cloud-providers', CloudProviderViewSet, basename='cloud-provider')
router.register('service-types', ServiceTypeViewSet, basename='service-type')

# 动作类接口需在 router 之前匹配，避免被 servers/{pk}/ 的 detail 路由吞掉
urlpatterns = [
    path('servers/batch/update/', ServerBatchUpdateView.as_view(), name='server-batch-update'),
    path('servers/batch/delete/', ServerBatchDeleteView.as_view(), name='server-batch-delete'),
    path('servers/import/', ServerImportView.as_view(), name='server-import'),
    path('servers/import-template/', ServerImportTemplateView.as_view(), name='server-import-template'),
    path('servers/register-jumpserver/', ServerRegisterJumpServerView.as_view(), name='server-register-jumpserver'),
] + router.urls
