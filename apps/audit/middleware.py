import json
import logging

logger = logging.getLogger(__name__)

SKIP_PATHS = ('/api/schema', '/admin/jsi18n')
WRITE_METHODS = ('POST', 'PUT', 'PATCH', 'DELETE')


class AuditLogMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        if request.method in WRITE_METHODS and not request.path.startswith(SKIP_PATHS):
            self._record(request, response)

        return response

    def _record(self, request, response):
        from .models import AuditLog

        try:
            body = request.body.decode('utf-8', errors='replace')[:2000]
        except Exception:
            body = ''

        user = request.user if request.user.is_authenticated else None
        ip = request.META.get('HTTP_X_FORWARDED_FOR', request.META.get('REMOTE_ADDR', ''))
        if ',' in ip:
            ip = ip.split(',')[0].strip()

        AuditLog.objects.create(
            user=user,
            action=request.method,
            path=request.path,
            method=request.method,
            ip=ip or None,
            status_code=response.status_code,
            body=body,
        )
