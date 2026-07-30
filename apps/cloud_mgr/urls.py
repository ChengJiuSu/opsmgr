from rest_framework.routers import DefaultRouter

from .views import CertificateViewSet, CloudAccountViewSet, DnsRecordViewSet, DomainViewSet

router = DefaultRouter()
router.register('accounts', CloudAccountViewSet, basename='cloud-account')
router.register('domains', DomainViewSet, basename='cloud-domain')
router.register('dns-records', DnsRecordViewSet, basename='cloud-dns-record')
router.register('certificates', CertificateViewSet, basename='cloud-certificate')

urlpatterns = router.urls
