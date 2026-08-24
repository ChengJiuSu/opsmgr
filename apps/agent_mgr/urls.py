from rest_framework.routers import DefaultRouter

from .views import AgentNodeViewSet, AgentRouteViewSet

router = DefaultRouter()
router.register('agents', AgentNodeViewSet, basename='agent')
router.register('agent-routes', AgentRouteViewSet, basename='agent-route')

urlpatterns = router.urls
