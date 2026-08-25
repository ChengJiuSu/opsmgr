from django.urls import path
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView, TokenVerifyView

from .views import (
    ChangePasswordView,
    LoginView,
    LogoutView,
    MeView,
    MfaActivateView,
    MfaDisableView,
    MfaRecoveryCodesView,
    MfaSetupView,
    MfaStatusView,
    MfaVerifyView,
    UserViewSet,
)

router = DefaultRouter()
router.register('users', UserViewSet, basename='user')

urlpatterns = [
    path('login/', LoginView.as_view(), name='login'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('token/verify/', TokenVerifyView.as_view(), name='token_verify'),
    path('me/', MeView.as_view(), name='me'),
    path('change-password/', ChangePasswordView.as_view(), name='change_password'),
    # ── MFA（二次认证）──
    path('mfa/verify/', MfaVerifyView.as_view(), name='mfa_verify'),
    path('mfa/status/', MfaStatusView.as_view(), name='mfa_status'),
    path('mfa/setup/', MfaSetupView.as_view(), name='mfa_setup'),
    path('mfa/activate/', MfaActivateView.as_view(), name='mfa_activate'),
    path('mfa/disable/', MfaDisableView.as_view(), name='mfa_disable'),
    path('mfa/recovery-codes/', MfaRecoveryCodesView.as_view(), name='mfa_recovery_codes'),
] + router.urls
