from django.urls import path

from .views import DashboardOverviewView, DashboardStatsView

urlpatterns = [
    path('stats/', DashboardStatsView.as_view(), name='dashboard-stats'),
    path('overview/', DashboardOverviewView.as_view(), name='dashboard-overview'),
]
