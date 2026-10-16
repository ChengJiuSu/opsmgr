"""消息通知配置面接口。

权限走独立模块 `module_perm('notify')`（可选档 none / read / write，见 roles.py）：

- read：看规则、打码后的机器人、发送记录——排查「为什么没收到」用；
- write：新增 / 修改 / 删除规则与机器人、发测试消息。模块没有 read_create 档，
  POST 按可选档向上取整到 write——新增一条规则就是把执行结果发到一个新的群，
  与改规则同等敏感；
- 写操作叠 sudo：群机器人 token 等于往群里发消息的凭据。

不再挂在 `system` 上：系统配置含 LDAP / SMTP 口令，为了配一条告警规则把这些
一起给出去，授权只能按最危险的那一项来给。
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

PERMISSIONS = [module_perm('notify'), SudoRequired]


def _bool_param(request, name):
    value = request.query_params.get(name)
    return None if value not in ('true', 'false') else value == 'true'


class NotifyEventTypeView(APIView):
    """GET /api/notify/events/ —— 可订阅的通知事件清单（代码里注册的）。"""

    permission_classes = PERMISSIONS

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request):
        return ok(events.choices())


class NotifyChatOptionsView(APIView):
    """GET /api/notify/chats/ —— 规则表单的「应用机器人会话」下拉。

    会话白名单的管理接口在 `/api/feishu/channels/`，要 system 档；只为了在规则里
    选一个会话就要求 system，等于把系统配置一起给出去。这里只回下拉需要的几个字段。
    """

    permission_classes = PERMISSIONS

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request):
        from apps.im_mgr.models import FeishuChannel

        return ok([
            {'id': c.id, 'name': c.name, 'chat_type': c.chat_type,
             'chat_type_display': c.get_chat_type_display(), 'enabled': c.enabled}
            for c in FeishuChannel.objects.order_by('name')
        ])


class WebhookRobotViewSet(viewsets.ModelViewSet):
    """群机器人（飞书自定义机器人 Webhook）。"""

    queryset = WebhookRobot.objects.all()
    serializer_class = WebhookRobotSerializer
    permission_classes = PERMISSIONS
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
    permission_classes = PERMISSIONS
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
    permission_classes = PERMISSIONS
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
