from rest_framework.routers import DefaultRouter

from .views import CredentialViewSet, ExecutionTaskViewSet

router = DefaultRouter()
router.register('tasks', ExecutionTaskViewSet, basename='exec-task')
router.register('credentials', CredentialViewSet, basename='credential')

urlpatterns = router.urls
