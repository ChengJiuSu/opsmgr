from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import TopoCanvasViewSet, TopoMetaView

router = DefaultRouter()
router.register('canvases', TopoCanvasViewSet, basename='topo-canvas')

urlpatterns = [
    # 元信息接口放 router 之前：避免被 router 的通配路由吃掉
    path('meta/', TopoMetaView.as_view(), name='topo-meta'),
    path('', include(router.urls)),
]
