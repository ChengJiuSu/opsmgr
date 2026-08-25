import logging

from rest_framework.response import Response
from rest_framework.views import exception_handler

logger = logging.getLogger(__name__)


def ok(data=None, msg='success'):
    return Response({'code': 0, 'msg': msg, 'data': data})


def fail(msg='error', code=1, status=400, data=None):
    # data 供需要附带明细的失败场景使用（如导入逐行校验错误），默认保持 None
    return Response({'code': code, 'msg': msg, 'data': data}, status=status)


def custom_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is not None:
        response.data = {
            'code': response.status_code,
            'msg': _extract_msg(response.data),
            'data': None,
        }
        # 机器可读错误码（DRF 异常的 default_code，如 mfa_enrollment_required）：
        # 前端靠它区分「该跳绑定页」与普通 403，不必去匹配提示文案
        error_code = _extract_code(exc)
        if error_code:
            response.data['error_code'] = error_code
    return response


def _extract_code(exc):
    codes = getattr(exc, 'get_codes', None)
    if codes is None:
        return None
    try:
        value = codes()
    except Exception:  # noqa: BLE001 - 取不到错误码不影响主响应
        return None
    return value if isinstance(value, str) else None


def _extract_msg(data):
    if isinstance(data, str):
        return data
    if isinstance(data, list):
        return str(data[0]) if data else 'error'
    if isinstance(data, dict):
        for v in data.values():
            return _extract_msg(v)
    return str(data)
