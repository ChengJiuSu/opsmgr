from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from ops_manager.health import healthz, readyz

urlpatterns = [
    path('', RedirectView.as_view(url='/api/docs/', permanent=False)),
    # 容器探针（无需认证）。healthz 不碰数据库，readyz 才探依赖，理由见 health.py。
    # ⚠ 走的是 Django 的 ALLOWED_HOSTS 校验：容器部署时 ALLOWED_HOSTS 必须含
    #   127.0.0.1，否则 healthcheck 恒得 400 Bad Request，容器一直是 unhealthy。
    path('healthz/', healthz, name='healthz'),
    path('readyz/', readyz, name='readyz'),
    path('admin/', admin.site.urls),
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('api/auth/', include('apps.auth_mgr.urls')),
    path('api/assets/', include('apps.asset_mgr.urls')),
    path('api/config/', include('apps.config_mgr.urls')),
    path('api/audit/', include('apps.audit.urls')),
    path('api/system/', include('apps.system_cfg.urls')),
    path('api/dashboard/', include('apps.dashboard.urls')),
    path('api/exec/', include('apps.task_exec.urls')),
    path('api/exec/', include('apps.method_mgr.urls')),
    path('api/artifacts/', include('apps.artifact_mgr.urls')),
    path('api/cloud/', include('apps.cloud_mgr.urls')),
    path('api/portal/', include('apps.portal_mgr.urls')),
    path('api/topo/', include('apps.topo_mgr.urls')),
    path('api/feishu/', include('apps.im_mgr.urls')),
    path('api/notify/', include('apps.notify_mgr.urls')),
    path('api/', include('apps.agent_mgr.urls')),
]
