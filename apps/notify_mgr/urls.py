from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import NotifyDeliveryViewSet, NotifyEventTypeView, NotifyRuleViewSet, WebhookRobotViewSet

router = DefaultRouter()
router.register('robots', WebhookRobotViewSet, basename='notify-robot')
router.register('rules', NotifyRuleViewSet, basename='notify-rule')
router.register('deliveries', NotifyDeliveryViewSet, basename='notify-delivery')

urlpatterns = [
    path('events/', NotifyEventTypeView.as_view(), name='notify-events'),
] + router.urls
