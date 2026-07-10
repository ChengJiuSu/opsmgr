from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import CredentialViewSet, ExecutionTaskViewSet, ExecVariableViewSet, PlaybookListView

router = DefaultRouter()
router.register('tasks', ExecutionTaskViewSet, basename='exec-task')
router.register('credentials', CredentialViewSet, basename='credential')
router.register('variables', ExecVariableViewSet, basename='exec-variable')

urlpatterns = [
    path('playbooks/', PlaybookListView.as_view(), name='exec-playbooks'),
] + router.urls
