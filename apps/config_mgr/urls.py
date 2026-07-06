from django.urls import path

from .views import (
    AllocationOverviewView,
    BatchAssignView,
    ServiceMarkView,
    ServicePlanView,
    ServiceUnplanView,
)

urlpatterns = [
    path('allocation/assign/', BatchAssignView.as_view(), name='allocation-assign'),
    path('allocation/overview/', AllocationOverviewView.as_view(), name='allocation-overview'),
    # 服务类型规划：规划（planned）/ 状态流转 / 取消规划
    path('services/plan/', ServicePlanView.as_view(), name='service-plan'),
    path('services/mark/', ServiceMarkView.as_view(), name='service-mark'),
    path('services/unplan/', ServiceUnplanView.as_view(), name='service-unplan'),
]
