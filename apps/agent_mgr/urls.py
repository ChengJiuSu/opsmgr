from rest_framework.routers import DefaultRouter

from .views import AgentNodeViewSet

router = DefaultRouter()
router.register('agents', AgentNodeViewSet, basename='agent')

urlpatterns = router.urls
