from rest_framework.routers import DefaultRouter

from .views import (
    ChannelViewSet,
    CloudProviderViewSet,
    EnvironmentViewSet,
    ProjectGroupViewSet,
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

urlpatterns = router.urls
