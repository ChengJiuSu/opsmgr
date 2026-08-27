"""接口 / Webhook 触发任务流执行（免登录，走 API 凭证）。

给两类调用方用：CI 构建完成后触发一条部署任务流、外部系统的 Webhook。校验链
（key + IP 白名单 + 作用域 + 可选签名）全在 apps/auth_mgr/apikey.py，这里只
负责「能不能这样触发」的业务约束：

- 只接受 `default_select_all=True` 的任务：接口调用方没法像执行弹窗那样逐模块
  勾选主机，关掉默认全选的任务本就是要求人工确认，接口不能绕过这条；
- 生产环境任务必须在请求体里显式带 `confirm_production: true`：机器触发没有
  「弹窗再确认」这一步，把确认动作前移到调用方脚本里，避免一条错配的 Webhook
  静默推平生产。

主机范围恒为「任务维度匹配到的全部主机」，与执行弹窗默认全选一致。
"""
import logging

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework.views import APIView

from apps.auth_mgr.apikey import ApiKeyAuthentication, ApiKeyThrottle, scope_required
from common.response import fail, ok

from . import trigger
from .models import ExecutionTask
from .serializers import ExecutionTaskSerializer

logger = logging.getLogger(__name__)


class TaskTriggerView(APIView):
    """POST /api/exec/tasks/<id>/trigger/ —— 凭 API 凭证发起一次任务流执行。"""

    authentication_classes = [ApiKeyAuthentication]
    permission_classes = [scope_required('task:trigger')]
    throttle_classes = [ApiKeyThrottle]

    @extend_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT)
    def post(self, request, pk):
        task = ExecutionTask.objects.filter(pk=pk).first()
        if task is None:
            return fail('任务不存在', status=404)

        if not task.default_select_all:
            return fail(f'任务「{task.name}」已关闭「默认全选主机」，'
                        f'需在页面上人工勾选主机后执行，不支持接口触发')

        if task.environment and task.environment.is_prod \
                and request.data.get('confirm_production') is not True:
            return fail(f'任务「{task.name}」目标环境为生产，'
                        f'接口触发必须在请求体显式带 confirm_production: true')

        error = trigger.preflight(task)
        if error:
            return fail(error)

        steps = list(task.steps.all())
        selected_global = list(task.matched_servers())
        credential = request.auth
        log, error = trigger.launch(
            task, steps, selected_global, picked={},
            triggered_by=request.user, trigger_type='api',
        )
        if error:
            return fail(error)

        logger.warning('API 凭证 %s 触发任务流 %s（%s 个模块，%s 台主机，log=%s）',
                       credential.key_id, task.name, len(steps), len(selected_global), log.id)
        return ok(
            {'task': ExecutionTaskSerializer(task).data, 'log_id': log.id,
             'step_count': len(steps), 'server_count': len(selected_global)},
            msg=f'已发起执行（{len(steps)} 个模块，{task.get_exec_mode_display()}）',
        )
