from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    TopoCanvasViewSet,
    TopoHealthView,
    TopoImpactView,
    TopoImportView,
    TopoMetaView,
)

router = DefaultRouter()
router.register('canvases', TopoCanvasViewSet, basename='topo-canvas')

urlpatterns = [
    # 显式路径放 router 之前：避免被 router 的通配路由吃掉
    path('meta/', TopoMetaView.as_view(), name='topo-meta'),
    path('import/', TopoImportView.as_view(), name='topo-import'),
    path('impact/', TopoImpactView.as_view(), name='topo-impact'),
    path('health/', TopoHealthView.as_view(), name='topo-health'),
    path('', include(router.urls)),
]
