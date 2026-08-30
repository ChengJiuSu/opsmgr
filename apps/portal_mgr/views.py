"""导航页配置接口。

权限口径分两层，刻意不一致：
- 管理接口（增删改查配置）走 `module_perm('dict')`，与其它基础字典同档；
- 聚合读接口 `/api/portal/config/` 只要求登录——导航页是全员入口，若也挂
  dict 档，没有字典权限的同事会打不开导航页，等于把一个门户做成了管理员工具。
  它吐的是内部系统名称与地址，本就是登录用户可见的信息。
"""

from django.db.models import ProtectedError
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.auth_mgr.permissions import module_perm
from common.response import fail, ok

from .models import (
    PortalBlock,
    PortalNavGroup,
    PortalNavItem,
    PortalNotice,
    PortalSetting,
    PortalSystem,
)
from .serializers import (
    PortalBlockSerializer,
    PortalNavGroupSerializer,
    PortalNavItemSerializer,
    PortalNoticeSerializer,
    PortalSettingSerializer,
    PortalSystemSerializer,
    build_config,
)


class BasePortalViewSet(viewsets.ModelViewSet):
    """导航页配置通用 ViewSet：启用/关键字筛选 + 被引用删除保护。"""

    permission_classes = [module_perm('dict')]
    search_fields = ('name',)
    protected_hint = '条记录'

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        enabled = params.get('enabled')
        keyword = params.get('keyword')
        if enabled in ('true', 'false'):
            qs = qs.filter(enabled=enabled == 'true')
        if keyword:
            from django.db.models import Q
            cond = Q()
            for f in self.search_fields:
                cond |= Q(**{f'{f}__icontains': keyword})
            qs = qs.filter(cond)
        return qs

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        try:
            return super().destroy(request, *args, **kwargs)
        except ProtectedError as exc:
            count = len(exc.protected_objects)
            return fail(f'「{instance}」仍被 {count} {self.protected_hint}引用，无法删除')


class PortalBlockViewSet(BasePortalViewSet):
    queryset = PortalBlock.objects.all()
    serializer_class = PortalBlockSerializer
    search_fields = ('title', 'code', 'tag')
    # 区块被系统卡片与导航项以 PROTECT 引用
    protected_hint = '条系统 / 导航项'


class PortalSystemViewSet(BasePortalViewSet):
    queryset = PortalSystem.objects.select_related('block').all()
    serializer_class = PortalSystemSerializer
    search_fields = ('name', 'abbr', 'domain', 'description')

    def get_queryset(self):
        qs = super().get_queryset()
        block = self.request.query_params.get('block')
        if block:
            # 字典外键筛选：纯数字按 id，否则按 code
            qs = qs.filter(block_id=block) if str(block).isdigit() else qs.filter(block__code=block)
        return qs


class PortalNavGroupViewSet(BasePortalViewSet):
    queryset = PortalNavGroup.objects.prefetch_related('items__block').all()
    serializer_class = PortalNavGroupSerializer
    search_fields = ('name', 'en_name')
    protected_hint = '个导航项'


class PortalNavItemViewSet(BasePortalViewSet):
    queryset = PortalNavItem.objects.select_related('group', 'block').all()
    serializer_class = PortalNavItemSerializer
    search_fields = ('label',)

    def get_queryset(self):
        qs = super().get_queryset()
        group = self.request.query_params.get('group')
        if group:
            qs = qs.filter(group_id=group)
        return qs


class PortalNoticeViewSet(BasePortalViewSet):
    queryset = PortalNotice.objects.prefetch_related('systems').all()
    serializer_class = PortalNoticeSerializer
    search_fields = ('title', 'code', 'body')


class PortalSettingView(APIView):
    """导航页设置（单例）：GET 读取，PUT 覆盖更新。"""

    permission_classes = [module_perm('dict')]

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request):
        return ok(PortalSettingSerializer(PortalSetting.load()).data)

    @extend_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT)
    def put(self, request):
        # 单例改的是全局展示口径，按「改」判档，不走 POST→read_create 的取整
        if not module_perm('dict', min_level='write')().has_permission(request, self):
            return fail('没有修改导航页设置的权限', status=403)
        instance = PortalSetting.load()
        ser = PortalSettingSerializer(instance, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        return ok(ser.data)


class PortalConfigView(APIView):
    """导航页启动时拉取的整份配置。全员可读，见模块 docstring。"""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request):
        return ok(build_config())
