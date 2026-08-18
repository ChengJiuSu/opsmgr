from django.urls import path
from rest_framework.routers import DefaultRouter

from .log_files import TaskLogFileContentView, TaskLogFileDownloadView, TaskLogFileListView
from .views import CredentialViewSet, ExecutionTaskViewSet, ExecVariableViewSet, PlaybookListView

router = DefaultRouter()
router.register('tasks', ExecutionTaskViewSet, basename='exec-task')
router.register('credentials', CredentialViewSet, basename='credential')
router.register('variables', ExecVariableViewSet, basename='exec-variable')

urlpatterns = [
    path('playbooks/', PlaybookListView.as_view(), name='exec-playbooks'),
    # 落盘日志与执行现场归档的目录浏览（仅管理员，只读）
    path('log-files/', TaskLogFileListView.as_view(), name='exec-log-files'),
    path('log-files/content/', TaskLogFileContentView.as_view(), name='exec-log-file-content'),
    path('log-files/download/', TaskLogFileDownloadView.as_view(), name='exec-log-file-download'),
] + router.urls
