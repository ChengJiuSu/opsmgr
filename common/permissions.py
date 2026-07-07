"""通用权限类。"""
from rest_framework.permissions import SAFE_METHODS, BasePermission


class IsAdminOrReadOnly(BasePermission):
    """管理员可写，其余用户只读。"""

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return bool(request.user and request.user.is_authenticated)
        return bool(request.user and request.user.is_staff)


class IsAdminOrReadCreate(BasePermission):
    """管理员全量读写；其余已认证用户可读、可新增（POST），不可修改/删除。

    注意：POST 放行覆盖 ViewSet 上所有 POST 自定义 action，
    仅适用于「POST 即新增」的纯数据 ViewSet。
    """

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        if request.method in SAFE_METHODS or request.method == 'POST':
            return True
        return bool(request.user.is_staff)


class IsAdmin(BasePermission):
    """仅管理员可访问。"""

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_staff)
