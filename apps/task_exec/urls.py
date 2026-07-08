from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import CredentialViewSet, ExecutionTaskViewSet, PlaybookListView

router = DefaultRouter()
router.register('tasks', ExecutionTaskViewSet, basename='exec-task')
router.register('credentials', CredentialViewSet, basename='credential')

urlpatterns = [
    path('playbooks/', PlaybookListView.as_view(), name='exec-playbooks'),
] + router.urls
