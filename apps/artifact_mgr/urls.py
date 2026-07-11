from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import ArtifactDownloadView, ArtifactReportView, ArtifactViewSet

router = DefaultRouter()
router.register('', ArtifactViewSet, basename='artifact')

urlpatterns = [
    path('report/', ArtifactReportView.as_view(), name='artifact-report'),
    path('<int:pk>/download/', ArtifactDownloadView.as_view(), name='artifact-download'),
] + router.urls
