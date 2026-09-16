from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import FeishuAppCallbackSlugView, FeishuAppView, FeishuChannelViewSet

router = DefaultRouter()
router.register('channels', FeishuChannelViewSet, basename='feishu-channel')

urlpatterns = [
    path('app/', FeishuAppView.as_view(), name='feishu-app'),
    path('app/rotate-slug/', FeishuAppCallbackSlugView.as_view(), name='feishu-app-rotate-slug'),
] + router.urls
