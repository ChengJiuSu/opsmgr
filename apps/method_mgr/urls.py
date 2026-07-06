from rest_framework.routers import DefaultRouter

from .views import MethodViewSet

router = DefaultRouter()
router.register('methods', MethodViewSet, basename='method')

urlpatterns = router.urls
