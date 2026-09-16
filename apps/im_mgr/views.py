"""飞书配置面接口（管理员维护，回调入口不在这里）。

权限统一挂 `module_perm('system', min_level='write')` + `SudoRequired`：

- **不新开权限模块**：飞书应用配置与 LDAP / SMTP 同一个风险面——含连接口令、
  且直接影响登录信任链（改 app_id 指向别的飞书应用 + 打开自动建号 = 任意人可
  登录），按 roles.py 的拆分判据本就归 `system`；
- **min_level='write' 而不是按方法推导**：读到的就是 App ID、回调地址与 IP
  白名单，等于一份「怎么打这两条公网入口」的说明书，GET 也不能对所有人开；
- **sudo**：改这里等于改安全边界，而登录那次 MFA 可能是 60 分钟前的事。
"""
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import viewsets
from rest_framework.views import APIView

from apps.auth_mgr.permissions import module_perm
from common.pagination import StandardPagination
from common.response import ok
from common.sudo import SudoRequired

from .models import FeishuApp, FeishuChannel
from .serializers import FeishuAppSerializer, FeishuChannelSerializer

CONFIG_PERMISSIONS = [module_perm('system', min_level='write'), SudoRequired]


class FeishuAppView(APIView):
    """GET / PUT /api/feishu/app/ —— 飞书应用配置（单例）。

    单例用 APIView 而不是 ViewSet：列表/详情/新增/删除这几个动作在这里都没有
    语义，暴露出来只会让调用方需要先知道「id 恒为 1」这种内部约定。
    """

    permission_classes = CONFIG_PERMISSIONS

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request):
        app = FeishuApp.load()
        return ok(FeishuAppSerializer(app, context={'request': request}).data)

    @extend_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT)
    def put(self, request):
        app = FeishuApp.load()
        serializer = FeishuAppSerializer(
            app, data=request.data, partial=True, context={'request': request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return ok(serializer.data, msg='已保存。')


class FeishuAppCallbackSlugView(APIView):
    """POST /api/feishu/app/rotate-slug/ —— 轮换回调标识（换回调地址）。

    独立动作而不是让 PUT 改 `callback_slug`：换了之后飞书后台的事件订阅与卡片
    回调地址会立刻失效，必须是一次有意识的操作，不能夹在一次普通保存里发生。
    """

    permission_classes = CONFIG_PERMISSIONS

    @extend_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT)
    def post(self, request):
        app = FeishuApp.load()
        app.rotate_callback_slug()
        data = FeishuAppSerializer(app, context={'request': request}).data
        return ok(data, msg='回调地址已轮换，请到飞书开放平台同步更新事件订阅与卡片回调地址。')


class FeishuChannelViewSet(viewsets.ModelViewSet):
    """飞书会话白名单（群 / 单聊）。"""

    queryset = FeishuChannel.objects.all()
    serializer_class = FeishuChannelSerializer
    permission_classes = CONFIG_PERMISSIONS
    pagination_class = StandardPagination

    def get_queryset(self):
        qs = super().get_queryset()
        chat_type = self.request.query_params.get('chat_type')
        if chat_type:
            qs = qs.filter(chat_type=chat_type)
        enabled = self.request.query_params.get('enabled')
        if enabled in ('true', 'false'):
            qs = qs.filter(enabled=enabled == 'true')
        allow_execute = self.request.query_params.get('allow_execute')
        if allow_execute in ('true', 'false'):
            qs = qs.filter(allow_execute=allow_execute == 'true')
        keyword = self.request.query_params.get('keyword')
        if keyword:
            from django.db.models import Q

            qs = qs.filter(Q(name__icontains=keyword) | Q(chat_id__icontains=keyword))
        return qs
