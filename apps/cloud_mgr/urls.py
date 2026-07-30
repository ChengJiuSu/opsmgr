from rest_framework.routers import DefaultRouter

from .views import (
    CertDeploymentViewSet,
    CertificateViewSet,
    CloudAccountViewSet,
    DnsRecordViewSet,
    DomainViewSet,
)

router = DefaultRouter()
router.register('accounts', CloudAccountViewSet, basename='cloud-account')
router.register('domains', DomainViewSet, basename='cloud-domain')
router.register('dns-records', DnsRecordViewSet, basename='cloud-dns-record')
router.register('certificates', CertificateViewSet, basename='cloud-certificate')
router.register('cert-deployments', CertDeploymentViewSet, basename='cloud-cert-deployment')

urlpatterns = router.urls
