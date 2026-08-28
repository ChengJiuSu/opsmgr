"""云控管理 API。

- 云账号：仅管理员（含凭据连通性测试）；
- 主域名/解析记录：管理员可写、登录用户只读；
- 解析记录以本地库为准，增删改即时推送厂商 DNS；推送失败保留本地记录并
  标记 sync_status=failed，可重试；
- 域名 sync_records 从厂商侧全量拉取镜像到本地（忽略 _acme-challenge 记录）；
- 证书：acme.sh DNS 验证签发/续签（后台异步）、zip 下载、分发目标管理与推送。
"""
import copy
import io
import logging
import re
import zipfile

from django.db import transaction
from django.db.models import Q, Value
from django.db.models.functions import Concat
from django.http import HttpResponse
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from common.pagination import StandardPagination
from apps.auth_mgr.permissions import module_perm
from common.response import fail, ok
from common.sudo import SudoRequired, required_for

from . import dns_approval, dns_history, dns_risk
from .acme import supported_provider_codes
from .deploy import supported_target_types
from .models import (
    CertDeployment,
    Certificate,
    CloudAccount,
    DnsChangeLog,
    DnsChangeRequest,
    DnsRecord,
    Domain,
)
from .providers import DnsProviderError, DnsRecordData, get_dns_provider
from .serializers import (
    CertDeploymentSerializer,
    CertificateSerializer,
    CloudAccountSerializer,
    DnsChangeLogSerializer,
    DnsChangeRequestSerializer,
    DnsRecordSerializer,
    DomainSerializer,
)
from .tasks import dispatch_deploy, dispatch_issue

logger = logging.getLogger(__name__)

# 证书 DNS 验证记录前缀：同步时忽略（acme.sh 验证后自动删除，窗口期不入库）
ACME_CHALLENGE_PREFIX = '_acme-challenge'


def _is_acme_challenge(rr):
    return rr == ACME_CHALLENGE_PREFIX or rr.startswith(ACME_CHALLENGE_PREFIX + '.')


def _record_keyword_qs(keyword):
    """按关键字匹配解析记录：主机记录 / 完整子域名(FQDN) / 记录值 任一命中。

    FQDN 在库中不存物理列（DnsRecord.fqdn 为属性），用 Concat 注解参与匹配，
    这样搜 `test.linuxlab.space` 也能命中 rr=test 的记录。
    """
    return DnsRecord.objects.annotate(
        fqdn_calc=Concat('rr', Value('.'), 'domain__name'),
    ).filter(
        Q(rr__icontains=keyword)
        | Q(fqdn_calc__icontains=keyword)
        | Q(value__icontains=keyword)
    )


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
    # 配置面写操作要求 sudo 二次验证（common/sudo.py）：改了就能拿到更多东西，
    # 而登录那次 MFA 可能是 60 分钟前的事
    permission_classes = [module_perm('cloud_account'), SudoRequired]
    pagination_class = StandardPagination

    def get_queryset(self):
        qs = super().get_queryset()
        provider = self.request.query_params.get('provider')
        if provider:
            qs = qs.filter(provider_id=provider) if provider.isdigit() else qs.filter(provider__code=provider)
        enabled = self.request.query_params.get('enabled')
        if enabled in ('true', 'false'):
            qs = qs.filter(enabled=enabled == 'true')
        keyword = self.request.query_params.get('keyword')
        if keyword:
            qs = qs.filter(Q(name__icontains=keyword) | Q(access_key__icontains=keyword))
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
    # 删除会连带删掉该域名下全部本地解析镜像（厂商侧不动），要二次验证
    permission_classes = [module_perm('cloud'), required_for('DELETE')]
    pagination_class = StandardPagination

    def get_queryset(self):
        qs = super().get_queryset()
        account = self.request.query_params.get('cloud_account')
        if account and account.isdigit():
            qs = qs.filter(cloud_account_id=account)
        keyword = self.request.query_params.get('keyword')
        if keyword:
            # 主域名名称命中，或名下任一解析记录（子域名/FQDN/记录值）命中
            qs = qs.filter(
                Q(name__icontains=keyword)
                | Q(pk__in=_record_keyword_qs(keyword).values('domain_id'))
            )
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
                # 同步时自动给根域 / NS / MX 打保护标记（已标记的不取消，
                # 人工标上的保护不该被一次同步抹掉）
                if dns_risk.auto_protected(r.rr, r.type):
                    fields['protected'] = True
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
    """解析记录 CRUD，写操作即时推送厂商。

    **改 / 删要求 sudo 二次验证**：改一条 A 记录的影响面（全站流量瞬间被引走）
    比改一条系统配置大得多，而原来这里是全场唯一没有二次验证的写入口。新增不拦
    ——加一条新子域名不会动到现有解析。

    **每次写操作落一条变更留痕**（DnsChangeLog），改/删前先从厂商拉当前值。
    原来删记录是「先删云端，再 record.delete()」，删完本地行没了、audit 的 DELETE
    请求又没有 body，等于「被删的是什么值」在系统里查不到。
    """

    queryset = DnsRecord.objects.select_related('domain', 'domain__cloud_account', 'server').all()
    serializer_class = DnsRecordSerializer
    permission_classes = [module_perm('cloud'), required_for('PUT', 'PATCH', 'DELETE')]
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
            # 主机记录 / 完整子域名 / 记录值（按 IP 反查）任一命中
            qs = qs.filter(pk__in=_record_keyword_qs(keyword).values('pk'))
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

    def _reason(self):
        return (self.request.data.get('reason') or '') if hasattr(self.request, 'data') else ''

    def _approval_gate(self, target, action, existing=None):
        """高危变更转变更单，返回 Response（已拦下）或 None（可直接执行）。

        高危判定见 dns_risk.py：加一条新的测试子域名直接放行，改根域 A 记录或
        删 MX 才走审批。全都拦会让人把审批当障碍去想办法躲。

        高危时**强制填变更原因**：审批人要据此判断，而事后排障最省事的线索也是它。
        """
        if not dns_approval.approval_enabled():
            return None
        domain = target.domain or getattr(existing, 'domain', None)
        reasons = dns_risk.risk_reasons(target, action, domain=domain)
        if not reasons:
            return None
        reason = self._reason().strip()
        if not reason:
            return fail('该变更命中高危规则，需填写变更原因（reason）后提交审批：'
                        + '；'.join(reasons))
        change_request = dns_approval.create_request(
            domain, target, action, self.request.user, reason, existing=existing)
        logger.warning('DNS 高危变更已转审批：#%s %s %s.%s（%s）',
                       change_request.pk, action, change_request.target_rr,
                       domain.name, '；'.join(reasons))
        return ok(
            {'pending_approval': True,
             'change_request': DnsChangeRequestSerializer(change_request).data},
            msg='该变更属高危操作，已提交审批（' + '；'.join(reasons) + '），待管理员批准后生效。',
        )

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        target = DnsRecord(**serializer.validated_data)
        # 新建的记录若命中高危（根域、NS/MX/CAA/TXT、指向生产主机…）也要审批
        gate = self._approval_gate(target, 'create')
        if gate is not None:
            return gate
        self.perform_create(serializer)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        # 目标状态 = 现有记录叠上本次改动；用副本算，别把未审批的值写进 instance
        target = copy.copy(instance)
        for field, value in serializer.validated_data.items():
            setattr(target, field, value)
        gate = self._approval_gate(target, 'update', existing=instance)
        if gate is not None:
            return gate
        self.perform_update(serializer)
        return Response(serializer.data)

    def perform_create(self, serializer):
        # 根域 / NS / MX 自动打保护标记：这几类最没有争议，等人工去标就会漏
        record = serializer.save(
            protected=serializer.validated_data.get('protected')
            or dns_risk.auto_protected(serializer.validated_data.get('rr', ''),
                                       serializer.validated_data.get('type', '')))
        self._push(record, is_new=True)
        dns_history.log_change(
            record, 'create', self.request.user,
            after=dns_history.local_snapshot(record), reason=self._reason())

    def perform_update(self, serializer):
        # 前值必须在推送之前取：推完再拉就是新值了
        before, source = dns_history.remote_snapshot(serializer.instance)
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
        dns_history.log_change(
            record, 'update', self.request.user, before=before, before_source=source,
            after=dns_history.local_snapshot(record), reason=self._reason())

    def destroy(self, request, *args, **kwargs):
        record = self.get_object()
        gate = self._approval_gate(record, 'delete', existing=record)
        if gate is not None:
            return gate
        # 前值在删之前取：删完本地行就没了，云端也查不到
        before, source = dns_history.remote_snapshot(record)
        domain = record.domain
        if record.remote_record_id:
            try:
                get_dns_provider(record.domain.cloud_account).delete_record(
                    record.domain.name, record.remote_record_id,
                )
            except DnsProviderError as e:
                return fail(f'厂商侧删除失败，本地记录保留：{e}')
        record.delete()
        dns_history.log_change(
            record, 'delete', request.user, before=before, before_source=source,
            reason=self._reason(), domain=domain)
        return ok(msg='已删除（变更前值已留痕，可在「变更留痕」中一键回滚）。')

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
    permission_classes = [module_perm('cloud')]
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
        """签发前置校验：厂商支持 + 非 issuing（超时残留除外）。返回错误信息或 None。"""
        if cert.status == 'issuing' and not cert.issue_stuck:
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
    @action(detail=True, methods=['get'], permission_classes=[module_perm('cloud', min_level='write')])
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
            if deployment.status == 'deploying' and not deployment.deploy_stuck:
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
        # issue_stuck（issuing 超时）放行：执行线程已随进程重启消失，不放行这条记录
        # 就永久删不掉也重申请不了。
        if cert.status == 'issuing' and not cert.issue_stuck:
            minutes = int(Certificate.ISSUE_TIMEOUT.total_seconds() // 60)
            return fail(f'证书正在申请中，禁止删除。若卡住，{minutes} 分钟后可删除或重新申请。')
        cert.delete()
        return ok(msg='已删除（acme 侧续签记录一并失效，不影响已部署副本）。')


class CertDeploymentViewSet(viewsets.ModelViewSet):
    """证书分发目标：登记目标 + 触发分发（后台异步）。

    - deploy：单目标分发；deploying 状态防重入；未接入的目标类型明确拒绝；
    - 证书签发/续签成功后 auto_deploy 目标由 tasks.run_issue 自动触发；
    - 分发前置校验：证书已签发；执行通道由 deploy.py 按主机三态自动选路
      （直连本机起 ansible，agent 通道下发 cert_deploy 单元）。

    **写操作要求 sudo 二次验证**：`reload_cmd` 是在目标主机上执行的任意 shell
    命令，登记一条分发目标等于拿到该主机的远程命令执行面——和「连接凭据」同性质，
    闸门必须同级。原来只有 IsAdminOrReadOnly，是这套配置面里唯一漏配 sudo 的地方。

    `deploy` 动作单独放行（只要 cloud 模块 write，不要求 sudo）：它执行的是**已经登记好**的配置，
    真正需要复核的是配置本身；每次分发都弹验证框会把签发后自动分发的日常流程
    变成人工卡点。
    """

    queryset = CertDeployment.objects.select_related(
        'certificate', 'server', 'credential',
    ).all()
    serializer_class = CertDeploymentSerializer
    permission_classes = [module_perm('cloud'), SudoRequired]
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
        if deployment.status == 'deploying' and not deployment.deploy_stuck:
            return '该目标正在分发中，请等待完成。'
        if deployment.target_type not in supported_target_types():
            return (f'{deployment.get_target_type_display()}证书分发暂未接入'
                    f'（接口已预留），当前仅支持服务器。')
        cert = deployment.certificate
        if cert.status != 'issued' or not cert.cert_pem:
            return '证书尚未签发，请先申请证书。'
        return None

    @extend_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT)
    @action(detail=True, methods=['post'], permission_classes=[module_perm('cloud', min_level='write')])
    def deploy(self, request, pk=None):
        """分发证书到该目标（后台异步执行 ansible 推送 + 重载）。

        不要求 sudo：执行的是已登记配置，需要复核的是配置本身（见类说明）。
        """
        deployment = self.get_object()
        err = self._validate_deployable(deployment)
        if err:
            return fail(err)
        dispatch_deploy(deployment)
        return ok(self.get_serializer(deployment).data, msg='已提交分发，请稍后刷新查看结果。')

    def destroy(self, request, *args, **kwargs):
        deployment = self.get_object()
        if deployment.status == 'deploying' and not deployment.deploy_stuck:
            minutes = int(CertDeployment.DEPLOY_TIMEOUT.total_seconds() // 60)
            return fail(f'该目标正在分发中，禁止删除。若卡住，{minutes} 分钟后可删除或重新分发。')
        deployment.delete()
        return ok(msg='已删除分发目标（目标机上已部署的证书文件不受影响）。')


class DnsChangeLogViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin,
                          viewsets.GenericViewSet):
    """解析记录变更留痕：只读列表 + 一键回滚。

    只读不可删：能被操作人删掉的留痕等于没有留痕。清理交给按时间的运维脚本。
    """

    queryset = DnsChangeLog.objects.select_related('domain', 'record', 'operator', 'restored_by')
    serializer_class = DnsChangeLogSerializer
    permission_classes = [module_perm('cloud')]
    pagination_class = StandardPagination

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        for param, field in (('domain', 'domain_id'), ('record', 'record_id')):
            value = params.get(param)
            if value and value.isdigit():
                qs = qs.filter(**{field: value})
        action_param = params.get('action')
        if action_param:
            qs = qs.filter(action=action_param)
        keyword = params.get('keyword')
        if keyword:
            qs = qs.filter(Q(record_rr__icontains=keyword) | Q(domain_name__icontains=keyword))
        return qs

    @extend_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT)
    @action(detail=True, methods=['post'], permission_classes=[module_perm('cloud', min_level='write'), SudoRequired])
    def restore(self, request, pk=None):
        """按前值回滚该次变更（云端先动、本地后动，云端失败则整体放弃）。"""
        change = self.get_object()
        success, message = dns_history.restore(change, request.user)
        if not success:
            return fail(message)
        logger.warning('管理员 %s 回滚了 DNS 变更 #%s（%s %s.%s）',
                       request.user.username, change.pk, change.action,
                       change.record_rr, change.domain_name)
        return ok(self.get_serializer(change).data, msg=message)


class DnsChangeRequestViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin,
                              viewsets.GenericViewSet):
    """DNS 高危变更单：查看 + 审批 / 驳回 / 撤销。

    建单不走这里——高危变更由解析记录接口自动拦下转单（见
    DnsRecordViewSet._approval_gate），避免出现「绕过记录接口直接建单」的入口。
    """

    queryset = DnsChangeRequest.objects.select_related(
        'domain', 'record', 'requested_by', 'reviewed_by')
    serializer_class = DnsChangeRequestSerializer
    permission_classes = [module_perm('cloud')]
    pagination_class = StandardPagination

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        status_param = params.get('status')
        if status_param == 'pending':
            # 「待我审」口径：只列还能批的，过期的不算（批也批不了）
            qs = qs.filter(status=DnsChangeRequest.STATUS_PENDING,
                           expires_at__gt=timezone.now())
        elif status_param:
            qs = qs.filter(status=status_param)
        domain = params.get('domain')
        if domain and domain.isdigit():
            qs = qs.filter(domain_id=domain)
        return qs

    @extend_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT)
    @action(detail=True, methods=['post'], permission_classes=[module_perm('cloud', min_level='write'), SudoRequired])
    def approve(self, request, pk=None):
        """审批通过并立即执行。

        `emergency=true` 走紧急通道（申请人可自批，必须填 comment）——没有它，
        半夜故障没人审批，最后结果是大家互相开超级管理账号，审批流反而降低安全性。
        """
        change_request = self.get_object()
        emergency = request.data.get('emergency') is True
        success, message = dns_approval.apply_request(
            change_request, request.user, emergency=emergency,
            comment=request.data.get('comment') or '')
        if not success:
            return fail(message)
        return ok(self.get_serializer(change_request).data, msg=message)

    @extend_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT)
    @action(detail=True, methods=['post'], permission_classes=[module_perm('cloud', min_level='write'), SudoRequired])
    def reject(self, request, pk=None):
        change_request = self.get_object()
        success, message = dns_approval.reject(
            change_request, request.user, comment=request.data.get('comment') or '')
        if not success:
            return fail(message)
        return ok(self.get_serializer(change_request).data, msg=message)

    @extend_schema(request=None, responses=OpenApiTypes.OBJECT)
    @action(
        detail=True, methods=['post'],
        # 撤销自己的申请只需 cloud 读档：能提申请的人就该能撤回。
        # 「只能撤自己的」由 dns_approval.cancel 判（管理员可撤任意单）
        permission_classes=[module_perm('cloud', min_level='read')],
    )
    def cancel(self, request, pk=None):
        """撤销自己提交的变更单（管理员可撤任意单）。"""
        change_request = self.get_object()
        success, message = dns_approval.cancel(change_request, request.user)
        if not success:
            return fail(message)
        return ok(self.get_serializer(change_request).data, msg=message)
