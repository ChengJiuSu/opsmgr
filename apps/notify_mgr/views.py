"""消息通知配置面接口。

权限与飞书接入同一档：`module_perm('system', min_level='write')` + 写操作 sudo。
群机器人 token 等于往群里发消息的凭据，订阅规则决定生产失败会不会被看见，
都按系统配置对待；发送记录里有任务名与失败原因，读也只给同一档。
"""
from django.db.models import Q
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.views import APIView

from apps.auth_mgr.permissions import module_perm
from common.pagination import StandardPagination
from common.response import fail, ok
from common.sudo import SudoRequired

from . import dispatch, events
from .models import NotifyDelivery, NotifyRule, WebhookRobot
from .serializers import NotifyDeliverySerializer, NotifyRuleSerializer, WebhookRobotSerializer

CONFIG_PERMISSIONS = [module_perm('system', min_level='write'), SudoRequired]


def _bool_param(request, name):
    value = request.query_params.get(name)
    return None if value not in ('true', 'false') else value == 'true'


class NotifyEventTypeView(APIView):
    """GET /api/notify/events/ —— 可订阅的通知事件清单（代码里注册的）。"""

    permission_classes = CONFIG_PERMISSIONS

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request):
        return ok(events.choices())


class WebhookRobotViewSet(viewsets.ModelViewSet):
    """群机器人（飞书自定义机器人 Webhook）。"""

    queryset = WebhookRobot.objects.all()
    serializer_class = WebhookRobotSerializer
    permission_classes = CONFIG_PERMISSIONS
    pagination_class = StandardPagination

    def get_queryset(self):
        qs = super().get_queryset()
        enabled = _bool_param(self.request, 'enabled')
        if enabled is not None:
            qs = qs.filter(enabled=enabled)
        keyword = self.request.query_params.get('keyword')
        if keyword:
            qs = qs.filter(Q(name__icontains=keyword) | Q(remark__icontains=keyword))
        return qs

    @extend_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT)
    @action(detail=True, methods=['post'])
    def test(self, request, pk=None):
        """POST /robots/{id}/test/ —— 发一条测试消息，验证 token / 签名 / 关键词配置。"""
        error = dispatch.send_robot_test(self.get_object())
        if error:
            return fail(error)
        return ok(msg='测试消息已发送，请到群里查看。')


class NotifyRuleViewSet(viewsets.ModelViewSet):
    """订阅规则。"""

    queryset = (NotifyRule.objects.select_related('feishu_chat', 'webhook_robot')
                .prefetch_related('project_groups', 'channels', 'environments'))
    serializer_class = NotifyRuleSerializer
    permission_classes = CONFIG_PERMISSIONS
    pagination_class = StandardPagination

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        enabled = _bool_param(self.request, 'enabled')
        if enabled is not None:
            qs = qs.filter(enabled=enabled)
        if params.get('target_type'):
            qs = qs.filter(target_type=params['target_type'])
        if params.get('webhook_robot'):
            qs = qs.filter(webhook_robot_id=params['webhook_robot'])
        if params.get('feishu_chat'):
            qs = qs.filter(feishu_chat_id=params['feishu_chat'])
        keyword = params.get('keyword')
        if keyword:
            qs = qs.filter(Q(name__icontains=keyword) | Q(remark__icontains=keyword))
        return qs

    @extend_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT)
    @action(detail=True, methods=['post'])
    def test(self, request, pk=None):
        """POST /rules/{id}/test/ —— 按规则的发送方式给目标发一条测试消息。"""
        error = dispatch.send_test(self.get_object())
        if error:
            return fail(error)
        return ok(msg='测试消息已发送。')


class NotifyDeliveryViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """发送记录（只读）：排查「为什么没收到」。"""

    queryset = NotifyDelivery.objects.select_related('event')
    serializer_class = NotifyDeliverySerializer
    permission_classes = CONFIG_PERMISSIONS
    pagination_class = StandardPagination

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        if params.get('status'):
            qs = qs.filter(status=params['status'])
        if params.get('event'):
            qs = qs.filter(event__code=params['event'])
        if params.get('target_type'):
            qs = qs.filter(target_type=params['target_type'])
        keyword = params.get('keyword')
        if keyword:
            qs = qs.filter(Q(title__icontains=keyword) | Q(rule_name__icontains=keyword)
                           | Q(target_name__icontains=keyword))
        return qs
