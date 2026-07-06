import logging

from rest_framework.response import Response
from rest_framework.views import exception_handler

logger = logging.getLogger(__name__)


def ok(data=None, msg='success'):
    return Response({'code': 0, 'msg': msg, 'data': data})


def fail(msg='error', code=1, status=400):
    return Response({'code': code, 'msg': msg, 'data': None}, status=status)


def custom_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is not None:
        response.data = {
            'code': response.status_code,
            'msg': _extract_msg(response.data),
            'data': None,
        }
    return response


def _extract_msg(data):
    if isinstance(data, str):
        return data
    if isinstance(data, list):
        return str(data[0]) if data else 'error'
    if isinstance(data, dict):
        for v in data.values():
            return _extract_msg(v)
    return str(data)
