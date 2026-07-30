from rest_framework.routers import DefaultRouter

from .views import CloudAccountViewSet, DnsRecordViewSet, DomainViewSet

router = DefaultRouter()
router.register('accounts', CloudAccountViewSet, basename='cloud-account')
router.register('domains', DomainViewSet, basename='cloud-domain')
router.register('dns-records', DnsRecordViewSet, basename='cloud-dns-record')

urlpatterns = router.urls
