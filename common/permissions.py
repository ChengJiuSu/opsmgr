"""通用权限类。"""
from rest_framework.permissions import SAFE_METHODS, BasePermission


class IsAdminOrReadOnly(BasePermission):
    """管理员可写，其余用户只读。"""

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return bool(request.user and request.user.is_authenticated)
        return bool(request.user and request.user.is_staff)


class IsAdmin(BasePermission):
    """仅管理员可访问。"""

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_staff)
