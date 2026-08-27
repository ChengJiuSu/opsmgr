from django.urls import path
from rest_framework.routers import DefaultRouter

from .log_files import TaskLogFileContentView, TaskLogFileDownloadView, TaskLogFileListView
from .views import CredentialViewSet, ExecutionTaskViewSet, ExecVariableViewSet, PlaybookListView
from .webhook import TaskTriggerView

router = DefaultRouter()
router.register('tasks', ExecutionTaskViewSet, basename='exec-task')
router.register('credentials', CredentialViewSet, basename='credential')
router.register('variables', ExecVariableViewSet, basename='exec-variable')

urlpatterns = [
    # 机读触发入口（API 凭证鉴权，免登录）：必须排在 router 之前，
    # 否则会被 tasks/<pk>/ 的 detail 路由吃掉
    path('tasks/<int:pk>/trigger/', TaskTriggerView.as_view(), name='exec-task-trigger'),
    path('playbooks/', PlaybookListView.as_view(), name='exec-playbooks'),
    # 落盘日志与执行现场归档的目录浏览（仅管理员，只读）
    path('log-files/', TaskLogFileListView.as_view(), name='exec-log-files'),
    path('log-files/content/', TaskLogFileContentView.as_view(), name='exec-log-file-content'),
    path('log-files/download/', TaskLogFileDownloadView.as_view(), name='exec-log-file-download'),
] + router.urls
