"""构建制品 API：CI 上报、平台查看/删除、签名下载。"""
import hashlib
import hmac
import logging
import os

from django.conf import settings
from django.http import FileResponse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, viewsets
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from apps.auth_mgr.apikey import (
    ApiKeyAuthentication,
    ApiKeyThrottle,
    request_credential,
    scope_required,
)
from apps.auth_mgr.authentication import MfaJWTAuthentication
from common.ansible import resolve_within
from apps.auth_mgr.permissions import module_perm
from common.response import fail, ok

from .models import Artifact
from .serializers import ArtifactReportSerializer, ArtifactSerializer

logger = logging.getLogger('apps.artifact_mgr')


class ArtifactViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin,
                      mixins.DestroyModelMixin, viewsets.GenericViewSet):
    """制品列表/详情/删除：登录用户可查，管理员可删；新增只能由 CI 上报。"""

    queryset = Artifact.objects.select_related('project_group', 'channel', 'environment')
    serializer_class = ArtifactSerializer
    permission_classes = [module_perm('artifact')]

    DICT_FILTERS = (
        ('project_group', 'project_group'),
        ('channel', 'channel'),
        ('env', 'environment'),
    )

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        for param, field in self.DICT_FILTERS:
            value = params.get(param)
            if value:
                if value.isdigit():
                    qs = qs.filter(**{f'{field}_id': value})
                else:
                    qs = qs.filter(**{f'{field}__code': value})
        keyword = params.get('keyword')
        if keyword:
            from django.db.models import Q
            qs = qs.filter(Q(version__icontains=keyword) | Q(original_name__icontains=keyword)
                           | Q(jenkins_job__icontains=keyword))
        return qs

    def perform_destroy(self, instance):
        """删除记录并同步清理磁盘文件（所在版本目录清空后一并移除）。"""
        # 文件删除口径与保留期清理共用 Artifact.remove_file（两处各写一遍迟早分叉，
        # 分叉的表现是「记录删了文件还在」，磁盘被没人知道的孤儿文件慢慢吃掉）
        instance.delete()
        instance.remove_file()


class ArtifactReportView(APIView):
    """CI 上报制品：multipart 上传文件与元数据，X-API-KEY 鉴权（免登录）。

    流式写盘同时计算 md5；上报 md5 不一致时删除文件并报错；
    同 项目组/渠道/环境/版本 已存在则拒绝（409），避免制品被静默覆盖。

    鉴权：API 凭证（key + IP 白名单 + `artifact:report` 作用域 + 数据范围）。
    旧的全局 `artifact.api_key` 兜底已于废弃期结束后删除——那把 key 全脚本共用、
    无来源限制、轮换要同时改所有调用方，且它同时兼着下载签名密钥，轮换一次会顺带
    作废所有下载 URL。
    """

    authentication_classes = [ApiKeyAuthentication]
    permission_classes = [scope_required('artifact:report')]
    throttle_classes = [ApiKeyThrottle]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT)
    def post(self, request):
        serializer = ArtifactReportSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        pg, ch, env = data['project_group'], data['channel'], data['environment']
        upload = data['file']

        # 凭证数据范围：一把限定了项目组的 CI key 不该能往别的项目组塞包
        credential = request_credential(request)
        if credential is not None and not credential.allows_dims(pg.id, env.id):
            return fail(f'凭证「{credential.name}」的数据范围不含 {pg.code}/{env.code}'
                        f'（当前范围：{credential.scope_label}）', status=403)

        if Artifact.objects.filter(
            project_group=pg, channel=ch, environment=env, version=data['version'],
        ).exists():
            return fail(f'制品版本已存在：{data["version"]}（如需重传请先删除旧记录）', status=409)

        rel_path = os.path.join(pg.code, ch.code, env.code, data['version'], upload.name)
        try:
            dest = resolve_within(settings.ARTIFACT_ROOT, rel_path)
        except ValueError:
            return fail('非法的存储路径')
        os.makedirs(os.path.dirname(dest), exist_ok=True)

        digest = hashlib.md5()
        with open(dest, 'wb') as f:
            for chunk in upload.chunks():
                f.write(chunk)
                digest.update(chunk)
        md5 = digest.hexdigest()

        reported_md5 = (data.get('md5') or '').lower()
        if reported_md5 and reported_md5 != md5:
            os.remove(dest)
            return fail(f'md5 校验不一致：上报 {reported_md5}，实际 {md5}，文件已丢弃')

        artifact = Artifact.objects.create(
            project_group=pg, channel=ch, environment=env, version=data['version'],
            file_path=rel_path, original_name=upload.name, md5=md5, size=upload.size,
            jenkins_job=data.get('jenkins_job') or '', build_number=data.get('build_number'),
            remark=data.get('remark') or '',
        )
        logger.info('制品上报成功：%s/%s/%s %s (%s, %s bytes)',
                    pg.code, ch.code, env.code, artifact.version, md5, upload.size)
        return ok(ArtifactSerializer(artifact).data, msg='上报成功')


class ArtifactDownloadView(APIView):
    """制品下载：三种身份任一即可，按「谁在拉包」分开判。

    - 登录用户（JWT）：页面上点下载；
    - API 凭证 + `artifact:download` 作用域：内部脚本拉包，受 IP 白名单约束；
    - URL 签名参数：CD 目标主机免登录拉包（签名口径见 Artifact.download_sig），
      原始 key 不出现在 URL 与执行日志里。

    permission_classes 保持 AllowAny，三条身份在方法体里判：签名这条没有
    「认证类」可挂（凭据在 query 里），混进权限类会让另外两条的失败原因变成
    同一个 403，排障时分不清是 key 不对还是签名不对。
    """

    authentication_classes = [ApiKeyAuthentication, MfaJWTAuthentication]
    permission_classes = [AllowAny]
    throttle_classes = [ApiKeyThrottle]

    @extend_schema(responses=OpenApiTypes.BINARY)
    def get(self, request, pk):
        artifact = Artifact.objects.filter(pk=pk).first()
        if artifact is None:
            return fail('制品不存在', status=404)

        credential = request_credential(request)
        if credential is not None:
            if not credential.has_scope('artifact:download'):
                return fail(f'凭证「{credential.name}」缺少作用域：artifact:download', status=403)
            if not credential.allows_dims(artifact.project_group_id, artifact.environment_id):
                return fail(f'凭证「{credential.name}」的数据范围不含该制品'
                            f'（当前范围：{credential.scope_label}）', status=403)
        elif not (request.user and request.user.is_authenticated):
            sig = request.query_params.get('sig', '')
            expected = artifact.download_sig()
            if not (expected and sig and hmac.compare_digest(sig, expected)):
                return fail('下载签名校验失败', status=403)

        path = artifact.abs_path()
        if not os.path.isfile(path):
            return fail('制品文件已丢失，请联系管理员', status=404)
        return FileResponse(open(path, 'rb'), as_attachment=True, filename=artifact.original_name)
