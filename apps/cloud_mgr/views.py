"""云控管理 API。

- 云账号：仅管理员（含凭据连通性测试）；
- 主域名/解析记录：管理员可写、登录用户只读；
- 解析记录以本地库为准，增删改即时推送厂商 DNS；推送失败保留本地记录并
  标记 sync_status=failed，可重试；
- 域名 sync_records 从厂商侧全量拉取镜像到本地（忽略 _acme-challenge 记录）；
- 证书：acme.sh DNS 验证签发/续签（后台异步）、zip 下载、分发目标管理与推送。
"""
import io
import logging
import re
import zipfile

from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import viewsets
from rest_framework.decorators import action

from common.pagination import StandardPagination
from common.permissions import IsAdmin, IsAdminOrReadOnly
from common.response import fail, ok

from .acme import supported_provider_codes
from .deploy import supported_target_types
from .models import CertDeployment, Certificate, CloudAccount, DnsRecord, Domain
from .providers import DnsProviderError, DnsRecordData, get_dns_provider
from .serializers import (
    CertDeploymentSerializer,
    CertificateSerializer,
    CloudAccountSerializer,
    DnsRecordSerializer,
    DomainSerializer,
)
from .tasks import dispatch_deploy, dispatch_issue

logger = logging.getLogger(__name__)

# 证书 DNS 验证记录前缀：同步时忽略（acme.sh 验证后自动删除，窗口期不入库）
ACME_CHALLENGE_PREFIX = '_acme-challenge'


def _is_acme_challenge(rr):
    return rr == ACME_CHALLENGE_PREFIX or rr.startswith(ACME_CHALLENGE_PREFIX + '.')


def _to_provider_data(record):
    return DnsRecordData(
        rr=record.rr, type=record.type, value=record.value,
        ttl=record.ttl, line=record.line or 'default',
        remote_record_id=record.remote_record_id,
        enabled=record.enabled,
    )


class CloudAccountViewSet(viewsets.ModelViewSet):
    queryset = CloudAccount.objects.select_related('provider').all()
    serializer_class = CloudAccountSerializer
    permission_classes = [IsAdmin]
    pagination_class = StandardPagination

    def get_queryset(self):
        qs = super().get_queryset()
        provider = self.request.query_params.get('provider')
        if provider:
            qs = qs.filter(provider_id=provider) if provider.isdigit() else qs.filter(provider__code=provider)
        enabled = self.request.query_params.get('enabled')
        if enabled in ('true', 'false'):
            qs = qs.filter(enabled=enabled == 'true')
        return qs

    def destroy(self, request, *args, **kwargs):
        account = self.get_object()
        if account.domains.exists():
            return fail('该云账号下存在主域名，禁止删除（请先迁移或删除域名）。')
        account.delete()
        return ok(msg='已删除。')

    @action(detail=True, methods=['post'])
    def test_connection(self, request, pk=None):
        """凭据连通性测试：调厂商域名列表接口，返回账号下托管域名。"""
        account = self.get_object()
        try:
            domains = get_dns_provider(account).list_domains()
        except DnsProviderError as e:
            return fail(str(e))
        return ok({'domains': domains}, msg=f'连接成功，账号下共 {len(domains)} 个域名。')


class DomainViewSet(viewsets.ModelViewSet):
    queryset = Domain.objects.select_related('cloud_account', 'cloud_account__provider').all()
    serializer_class = DomainSerializer
    permission_classes = [IsAdminOrReadOnly]
    pagination_class = StandardPagination

    def get_queryset(self):
        qs = super().get_queryset()
        account = self.request.query_params.get('cloud_account')
        if account and account.isdigit():
            qs = qs.filter(cloud_account_id=account)
        keyword = self.request.query_params.get('keyword')
        if keyword:
            qs = qs.filter(name__icontains=keyword)
        return qs

    def destroy(self, request, *args, **kwargs):
        # 仅删本地管理数据，不动厂商侧解析（防误删线上解析）
        domain = self.get_object()
        count = domain.records.count()
        domain.delete()
        return ok(msg=f'已删除本地域名及 {count} 条本地解析记录（厂商侧解析未改动）。')

    @extend_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT)
    @action(detail=False, methods=['post'])
    def import_remote(self, request):
        """从云账号导入托管域名：{"cloud_account": id}，已存在的跳过。"""
        account_id = request.data.get('cloud_account')
        if not account_id:
            return fail('缺少参数 cloud_account')
        account = CloudAccount.objects.filter(pk=account_id).first()
        if account is None:
            return fail('云账号不存在')
        try:
            names = get_dns_provider(account).list_domains()
        except DnsProviderError as e:
            return fail(str(e))
        created = []
        with transaction.atomic():
            for name in names:
                _, is_new = Domain.objects.get_or_create(
                    name=name, defaults={'cloud_account': account},
                )
                if is_new:
                    created.append(name)
        return ok(
            {'imported': created, 'total': len(names)},
            msg=f'导入完成：新增 {len(created)} 个，账号下共 {len(names)} 个。',
        )

    @action(detail=True, methods=['post'])
    def sync_records(self, request, pk=None):
        """从厂商侧全量同步解析记录到本地（镜像）。

        - 忽略 _acme-challenge 证书验证记录；
        - 本地有 remote_record_id 但厂商侧已不存在的记录删除；
        - 本地 pending/failed（尚未推送成功）的记录保留。
        """
        domain = self.get_object()
        try:
            remote_records = get_dns_provider(domain.cloud_account).list_records(domain.name)
        except DnsProviderError as e:
            domain.status = 'abnormal'
            domain.save(update_fields=['status', 'updated_at'])
            return fail(str(e))

        stats = {'created': 0, 'updated': 0, 'deleted': 0, 'skipped_acme': 0}
        with transaction.atomic():
            remote_ids = set()
            for r in remote_records:
                if _is_acme_challenge(r.rr):
                    stats['skipped_acme'] += 1
                    continue
                remote_ids.add(r.remote_record_id)
                fields = {
                    'rr': r.rr, 'type': r.type, 'value': r.value, 'ttl': r.ttl,
                    'line': r.line, 'enabled': r.enabled,
                    'sync_status': 'synced', 'sync_error': '',
                }
                obj = domain.records.filter(remote_record_id=r.remote_record_id).first()
                if obj is None:
                    # 本地手工建但尚未推送成功的同内容记录：认领厂商侧 ID
                    obj = domain.records.filter(
                        rr=r.rr, type=r.type, value=r.value, line=r.line,
                        remote_record_id='',
                    ).first()
                if obj:
                    for k, v in fields.items():
                        setattr(obj, k, v)
                    obj.remote_record_id = r.remote_record_id
                    obj.save()
                    stats['updated'] += 1
                else:
                    domain.records.create(remote_record_id=r.remote_record_id, **fields)
                    stats['created'] += 1
            deleted, _ = (
                domain.records.exclude(remote_record_id='')
                .exclude(remote_record_id__in=remote_ids)
                .delete()
            )
            stats['deleted'] = deleted
            domain.status = 'normal'
            domain.last_synced_at = timezone.now()
            domain.save(update_fields=['status', 'last_synced_at', 'updated_at'])
        return ok(stats, msg='同步完成：新增 {created}、更新 {updated}、删除 {deleted}。'.format(**stats))


class DnsRecordViewSet(viewsets.ModelViewSet):
    queryset = DnsRecord.objects.select_related('domain', 'domain__cloud_account', 'server').all()
    serializer_class = DnsRecordSerializer
    permission_classes = [IsAdminOrReadOnly]
    pagination_class = StandardPagination

    def get_queryset(self):
        qs = super().get_queryset()
        domain = self.request.query_params.get('domain')
        if domain and domain.isdigit():
            qs = qs.filter(domain_id=domain)
        rtype = self.request.query_params.get('type')
        if rtype:
            qs = qs.filter(type=rtype)
        keyword = self.request.query_params.get('keyword')
        if keyword:
            qs = qs.filter(rr__icontains=keyword)
        sync_status = self.request.query_params.get('sync_status')
        if sync_status:
            qs = qs.filter(sync_status=sync_status)
        return qs

    def _push(self, record, is_new):
        """推送到厂商 DNS，就地更新 sync 状态（不抛异常）。"""
        try:
            provider = get_dns_provider(record.domain.cloud_account)
            if is_new or not record.remote_record_id:
                record.remote_record_id = provider.add_record(record.domain.name, _to_provider_data(record))
            else:
                provider.update_record(record.domain.name, _to_provider_data(record))
            record.sync_status, record.sync_error = 'synced', ''
        except DnsProviderError as e:
            record.sync_status, record.sync_error = 'failed', str(e)
        record.save(update_fields=['remote_record_id', 'sync_status', 'sync_error', 'updated_at'])

    def perform_create(self, serializer):
        record = serializer.save()
        self._push(record, is_new=True)

    def perform_update(self, serializer):
        old_enabled = serializer.instance.enabled
        record = serializer.save()
        self._push(record, is_new=False)
        # 启停状态单独接口（部分厂商更新记录不改状态）
        if record.sync_status == 'synced' and record.enabled != old_enabled:
            try:
                get_dns_provider(record.domain.cloud_account).set_record_enabled(
                    record.domain.name, record.remote_record_id, record.enabled,
                )
            except DnsProviderError as e:
                record.sync_status, record.sync_error = 'failed', str(e)
                record.save(update_fields=['sync_status', 'sync_error', 'updated_at'])

    def destroy(self, request, *args, **kwargs):
        record = self.get_object()
        if record.remote_record_id:
            try:
                get_dns_provider(record.domain.cloud_account).delete_record(
                    record.domain.name, record.remote_record_id,
                )
            except DnsProviderError as e:
                return fail(f'厂商侧删除失败，本地记录保留：{e}')
        record.delete()
        return ok(msg='已删除。')

    @action(detail=True, methods=['post'])
    def retry_sync(self, request, pk=None):
        """重推 pending/failed 记录。"""
        record = self.get_object()
        if record.sync_status == 'synced':
            return fail('该记录已同步，无需重推。')
        self._push(record, is_new=not record.remote_record_id)
        if record.sync_status == 'failed':
            return fail(f'重推失败：{record.sync_error}')
        return ok(self.get_serializer(record).data, msg='重推成功。')


class CertificateViewSet(viewsets.ModelViewSet):
    """证书管理：申请/续签（acme.sh DNS 验证，后台异步）。

    - issue：pending/failed 首次申请；issued 也可手动触发（等价强制重签）；
    - renew：已签发证书强制重签（--force）；
    - download：证书/私钥/完整链打包 zip（含私钥，no-store），仅管理员；
    - issuing 状态禁止重复触发与删除。
    """

    queryset = Certificate.objects.select_related('cloud_account', 'cloud_account__provider').all()
    serializer_class = CertificateSerializer
    permission_classes = [IsAdminOrReadOnly]
    pagination_class = StandardPagination

    def get_queryset(self):
        qs = super().get_queryset()
        status_param = self.request.query_params.get('status')
        if status_param:
            qs = qs.filter(status=status_param)
        keyword = self.request.query_params.get('keyword')
        if keyword:
            qs = qs.filter(name__icontains=keyword)
        return qs

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    def _validate_issuable(self, cert):
        """签发前置校验：厂商支持 + 非 issuing。返回错误信息或 None。"""
        if cert.status == 'issuing':
            return '证书正在申请中，请等待完成。'
        code = cert.cloud_account.provider.code
        if code not in supported_provider_codes():
            return (f'云厂商 {cert.cloud_account.provider.name}({code}) 暂不支持证书 DNS 验证，'
                    f'当前支持：{"、".join(supported_provider_codes())}')
        return None

    @extend_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT)
    @action(detail=True, methods=['post'])
    def issue(self, request, pk=None):
        """申请证书（后台异步，DNS 验证需数分钟）。已签发的等价强制重签。"""
        cert = self.get_object()
        err = self._validate_issuable(cert)
        if err:
            return fail(err)
        dispatch_issue(cert, force=cert.status == 'issued')
        return ok(self.get_serializer(cert).data, msg='已提交申请，DNS 验证需数分钟，请稍后刷新查看。')

    @extend_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT)
    @action(detail=True, methods=['post'])
    def renew(self, request, pk=None):
        """强制续签（--force 重签，签发日期与过期时间刷新）。"""
        cert = self.get_object()
        if cert.status != 'issued':
            return fail('仅已签发证书可续签，未签发请用「申请」。')
        err = self._validate_issuable(cert)
        if err:
            return fail(err)
        dispatch_issue(cert, force=True)
        return ok(self.get_serializer(cert).data, msg='已提交续签，请稍后刷新查看。')

    @extend_schema(responses={200: OpenApiTypes.BINARY})
    @action(detail=True, methods=['get'], permission_classes=[IsAdmin])
    def download(self, request, pk=None):
        """完整证书材料打包 zip 下载（含私钥），仅管理员。

        包内统一带一层以证书名称命名的目录（解压不散落、多证书可并存）：
        <名称>/<名称>.crt（证书）、.key（私钥）、.fullchain.pem（完整链）、README.txt。
        """
        cert = self.get_object()
        if cert.status != 'issued' or not cert.cert_pem:
            return fail('证书尚未签发，无可下载内容。')

        # 目录名与文件名取证书名称，去掉路径分隔符等不安全字符
        safe_name = re.sub(r'[^\w.*-]', '_', cert.name) or f'cert-{cert.pk}'
        readme = (
            f'证书名称：{cert.name}\n'
            f'域名：{", ".join(cert.domains)}\n'
            f'签发时间：{cert.issued_at.strftime("%Y-%m-%d %H:%M:%S") if cert.issued_at else "-"}\n'
            f'过期时间：{cert.expires_at.strftime("%Y-%m-%d %H:%M:%S") if cert.expires_at else "-"}\n'
            f'\n文件说明：\n'
            f'  {safe_name}.crt            证书（server_certificate）\n'
            f'  {safe_name}.key            私钥（请妥善保管，勿外传）\n'
            f'  {safe_name}.fullchain.pem  证书 + 中间链（Nginx ssl_certificate 用此文件）\n'
        )

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f'{safe_name}/{safe_name}.crt', cert.cert_pem)
            zf.writestr(f'{safe_name}/{safe_name}.key', cert.private_key_pem)
            if cert.chain_pem:
                zf.writestr(f'{safe_name}/{safe_name}.fullchain.pem', cert.chain_pem)
            zf.writestr(f'{safe_name}/README.txt', readme)

        response = HttpResponse(buffer.getvalue(), content_type='application/zip')
        response['Content-Disposition'] = f'attachment; filename="{safe_name}.zip"'
        # 含私钥，禁止任何中间层缓存
        response['Cache-Control'] = 'no-store'
        return response

    @extend_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT)
    @action(detail=True, methods=['post'])
    def deploy_all(self, request, pk=None):
        """分发到该证书全部已登记目标（跳过分发中与未接入类型）。"""
        cert = self.get_object()
        if cert.status != 'issued' or not cert.cert_pem:
            return fail('证书尚未签发，请先申请证书。')
        dispatched, skipped = [], []
        for deployment in cert.deployments.all():
            if deployment.status == 'deploying':
                skipped.append(f'{deployment} 正在分发中')
                continue
            if deployment.target_type not in supported_target_types():
                skipped.append(f'{deployment.get_target_type_display()} 暂未接入')
                continue
            dispatch_deploy(deployment)
            dispatched.append(deployment.id)
        if not dispatched:
            return fail('没有可分发的目标。' + ('跳过：' + '；'.join(skipped) if skipped else ''))
        return ok(
            {'dispatched': dispatched, 'skipped': skipped},
            msg=f'已提交 {len(dispatched)} 个目标的分发'
                + (f'，跳过 {len(skipped)} 个。' if skipped else '。'),
        )

    def destroy(self, request, *args, **kwargs):
        cert = self.get_object()
        if cert.status == 'issuing':
            return fail('证书正在申请中，禁止删除。')
        cert.delete()
        return ok(msg='已删除（acme 侧续签记录一并失效，不影响已部署副本）。')


class CertDeploymentViewSet(viewsets.ModelViewSet):
    """证书分发目标：登记目标 + 触发分发（后台异步）。

    - deploy：单目标分发；deploying 状态防重入；未接入的目标类型明确拒绝；
    - 证书签发/续签成功后 auto_deploy 目标由 tasks.run_issue 自动触发；
    - 分发前置校验：证书已签发、服务器为直连通道（agent 通道 v1 不支持）。
    """

    queryset = CertDeployment.objects.select_related(
        'certificate', 'server', 'credential',
    ).all()
    serializer_class = CertDeploymentSerializer
    permission_classes = [IsAdminOrReadOnly]
    pagination_class = StandardPagination

    def get_queryset(self):
        qs = super().get_queryset()
        certificate = self.request.query_params.get('certificate')
        if certificate and certificate.isdigit():
            qs = qs.filter(certificate_id=certificate)
        status_param = self.request.query_params.get('status')
        if status_param:
            qs = qs.filter(status=status_param)
        target_type = self.request.query_params.get('target_type')
        if target_type:
            qs = qs.filter(target_type=target_type)
        return qs

    def _validate_deployable(self, deployment):
        """分发前置校验，返回错误信息或 None。"""
        if deployment.status == 'deploying':
            return '该目标正在分发中，请等待完成。'
        if deployment.target_type not in supported_target_types():
            return (f'{deployment.get_target_type_display()}证书分发暂未接入'
                    f'（接口已预留），当前仅支持服务器。')
        cert = deployment.certificate
        if cert.status != 'issued' or not cert.cert_pem:
            return '证书尚未签发，请先申请证书。'
        return None

    @extend_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT)
    @action(detail=True, methods=['post'])
    def deploy(self, request, pk=None):
        """分发证书到该目标（后台异步执行 ansible 推送 + 重载）。"""
        deployment = self.get_object()
        err = self._validate_deployable(deployment)
        if err:
            return fail(err)
        dispatch_deploy(deployment)
        return ok(self.get_serializer(deployment).data, msg='已提交分发，请稍后刷新查看结果。')

    def destroy(self, request, *args, **kwargs):
        deployment = self.get_object()
        if deployment.status == 'deploying':
            return fail('该目标正在分发中，禁止删除。')
        deployment.delete()
        return ok(msg='已删除分发目标（目标机上已部署的证书文件不受影响）。')
