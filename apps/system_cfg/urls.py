from rest_framework.routers import DefaultRouter

from .views import SystemConfigViewSet

router = DefaultRouter()
router.register('configs', SystemConfigViewSet, basename='system-config')

urlpatterns = router.urls
