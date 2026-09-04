from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import ArtifactDownloadView, ArtifactReportView, ArtifactViewSet

router = DefaultRouter()
router.register('', ArtifactViewSet, basename='artifact')

urlpatterns = [
    path('report/', ArtifactReportView.as_view(), name='artifact-report'),
    path('<int:pk>/download/', ArtifactDownloadView.as_view(), name='artifact-download'),
    # 文件名作为路径末段：curl -O / wget 默认按 URL 末段命名，不认 Content-Disposition，
    # 带上它命令行下载才不用额外指定 -OJ 或 -o。上面那条无文件名的路由保留，
    # 已签发但还没用掉的旧 URL 走的是它。
    path('<int:pk>/download/<str:filename>', ArtifactDownloadView.as_view(),
         name='artifact-download-named'),
] + router.urls
