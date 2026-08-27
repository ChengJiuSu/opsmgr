import csv
import io

from django.core.exceptions import ValidationError
from django.core.validators import validate_ipv46_address
from django.db import transaction
from django.db.models import ProtectedError, Q
from django.http import HttpResponse
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.views import APIView

from apps.auth_mgr.apikey import (
    ApiKeyAuthentication,
    ApiKeyThrottle,
    request_credential,
    user_or_scope,
)
from apps.auth_mgr.authentication import MfaJWTAuthentication
from common.jumpserver import register_servers
from common.permissions import IsAdmin, IsAdminOrReadCreate
from common.sudo import SudoRequired
from common.sudo import required_for as sudo_required_for
from common.response import fail, ok

from .models import Channel, CloudProvider, Environment, ProjectGroup, Server, ServerService, ServiceType
from .serializers import (
    ChannelSerializer,
    CloudProviderSerializer,
    EnvironmentSerializer,
    ProjectGroupSerializer,
    ServerSerializer,
    ServiceTypeSerializer,
)

# CSV 导入导出列（顺序即模板列顺序）：字典外键列填字典编码（code），name/private_ip 必填
# CSV 列顺序（导出 / 模板 / 导入共用）。注意 channel 是 CMDB「渠道」字典，
# 执行通道是 channel_mode（auto/direct/agent）+ agent（Agent 名称，仅 agent 模式生效）。
CSV_COLUMNS = [
    'name', 'private_ip', 'public_ip', 'os', 'cpu', 'memory', 'status',
    'project_group', 'channel', 'environment', 'cloud_provider',
    'region', 'channel_mode', 'agent', 'ssh_port', 'ssh_user', 'remark',
]
# CSV 字典外键列 → (模型, 展示名)
CSV_DICT_FIELDS = {
    'project_group': (ProjectGroup, '项目组'),
    'channel': (Channel, '渠道'),
    'environment': (Environment, '环境'),
    'cloud_provider': (CloudProvider, '云厂商'),
}
# 批量编辑允许的普通字段（字典外键分配走「配置管理 → 服务器分配」的批量分配）
BATCH_UPDATE_FIELDS = ('status', 'os', 'region', 'ssh_user', 'ssh_port', 'remark',
                       'channel_mode', 'agent')


class BaseDictViewSet(viewsets.ModelViewSet):
    """CMDB 字典表通用 ViewSet：启用/关键字筛选 + 被引用删除保护。

    权限：已认证用户可查看/新增，修改与删除仅管理员。
    """

    permission_classes = [IsAdminOrReadCreate]
    # 删除保护提示中的引用对象单位（如「台服务器」「条主机服务记录」）
    protected_hint = '台服务器'

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        enabled = params.get('enabled')
        keyword = params.get('keyword')
        if enabled in ('true', 'false'):
            qs = qs.filter(enabled=enabled == 'true')
        if keyword:
            qs = qs.filter(Q(name__icontains=keyword) | Q(code__icontains=keyword))
        return qs

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        try:
            return super().destroy(request, *args, **kwargs)
        except ProtectedError as exc:
            # PROTECT 外键约束：字典项仍被引用时禁止删除，计数取自约束异常本身
            count = len(exc.protected_objects)
            return fail(f'「{instance.name}」仍被 {count} {self.protected_hint}引用，无法删除')


class ProjectGroupViewSet(BaseDictViewSet):
    queryset = ProjectGroup.objects.all()
    serializer_class = ProjectGroupSerializer


class ChannelViewSet(BaseDictViewSet):
    queryset = Channel.objects.all()
    serializer_class = ChannelSerializer


class EnvironmentViewSet(BaseDictViewSet):
    queryset = Environment.objects.all()
    serializer_class = EnvironmentSerializer


class CloudProviderViewSet(BaseDictViewSet):
    queryset = CloudProvider.objects.all()
    serializer_class = CloudProviderSerializer


class ServiceTypeViewSet(BaseDictViewSet):
    queryset = ServiceType.objects.all()
    serializer_class = ServiceTypeSerializer
    # 服务类型被 ServerService（及 method_mgr.Method）以 PROTECT 引用
    protected_hint = '条记录'


class ServerViewSet(viewsets.ModelViewSet):
    queryset = (
        Server.objects
        .select_related('project_group', 'channel', 'environment', 'cloud_provider', 'agent')
        .prefetch_related('services__service_type')
        .order_by('-created_at')
    )
    serializer_class = ServerSerializer
    # 已认证用户可查看/新增服务器，修改与删除仅管理员
    # 改/删还要 sudo 二次验证：channel_mode/agent 决定命令下发到哪台机器执行，
    # 与通道路由同性质。新增不拦——新加的主机不会改变已有主机的执行通道
    # 登录用户走原有权限；API 凭证（内部脚本）凭 asset:read 只读，
    # 且可见范围由凭证的项目组/环境收窄（见 get_queryset）
    authentication_classes = [ApiKeyAuthentication, MfaJWTAuthentication]
    permission_classes = [
        user_or_scope(IsAdminOrReadCreate, 'asset:read'),
        sudo_required_for('PUT', 'PATCH', 'DELETE'),
    ]
    throttle_classes = [ApiKeyThrottle]

    # 查询参数 → 字典外键字段映射（值为纯数字按 id 过滤，否则按 code 过滤）
    DICT_FILTERS = (
        ('env', 'environment'),
        ('project_group', 'project_group'),
        ('channel', 'channel'),
        ('cloud_provider', 'cloud_provider'),
    )

    def get_queryset(self):
        qs = super().get_queryset()
        # 机读凭证：先按凭证的数据范围收窄，再叠常规筛选。
        # 收窄可见集而不是拒绝请求——调用方忘了带筛选参数时拿到的是「我的全部」，
        # 不是「全公司的全部」
        credential = request_credential(self.request)
        if credential is not None:
            qs = credential.narrow(qs)
        params = self.request.query_params
        status = params.get('status')
        keyword = params.get('keyword')
        if status:
            qs = qs.filter(status=status)
        for param, field in self.DICT_FILTERS:
            value = params.get(param)
            if value:
                if value.isdigit():
                    qs = qs.filter(**{f'{field}_id': value})
                else:
                    qs = qs.filter(**{f'{field}__code': value})
        # 服务筛选：服务类型 / 服务状态 / 可执行（executable=true → 已规划或已部署，
        # 任务执行表单专用，与 task_exec 执行权限校验口径一致，offline 不算）。
        # 三者必须命中同一条 services 记录，故合并进单次 filter：分开 filter 会各自
        # JOIN，「A 已规划 + B 已下线」的主机会被 service_type=B&executable=true 误命中
        service_cond = {}
        service_type = params.get('service_type')
        service_status = params.get('service_status')
        if service_type:
            key = 'services__service_type_id' if service_type.isdigit() else 'services__service_type__code'
            service_cond[key] = service_type
        if service_status:
            service_cond['services__status'] = service_status
        if params.get('executable') == 'true':
            service_cond['services__status__in'] = ('planned', 'deployed')
        if service_cond:
            qs = qs.filter(**service_cond).distinct()
        # 已规划筛选：assigned=service_type → 仅返回有服务记录的主机（服务器分配页专用）
        if params.get('assigned') == 'service_type':
            qs = qs.filter(services__isnull=False).distinct()
        # 未分配筛选：unassigned=<维度字段名> / any（任一归属维度缺失）/ service_type（未规划任何服务）
        unassigned = params.get('unassigned')
        dict_fields = [field for _, field in self.DICT_FILTERS]
        if unassigned == 'any':
            cond = Q()
            for field in dict_fields:
                cond |= Q(**{f'{field}__isnull': True})
            qs = qs.filter(cond)
        elif unassigned in dict_fields:
            qs = qs.filter(**{f'{unassigned}__isnull': True})
        elif unassigned == 'service_type':
            qs = qs.filter(services__isnull=True)
        if keyword:
            qs = qs.filter(
                Q(name__icontains=keyword)
                | Q(private_ip__icontains=keyword)
                | Q(public_ip__icontains=keyword)
            )
        return qs

    @action(detail=False, methods=['get'])
    def stats(self, request):
        """服务器维度统计：按状态、环境、项目组分组计数。"""
        qs = self.get_queryset()
        by_status = {key: 0 for key, _ in Server.STATUS_CHOICES}
        by_env = {}
        by_project = {}
        for srv in qs.values('status', 'environment__name', 'project_group__name'):
            by_status[srv['status']] = by_status.get(srv['status'], 0) + 1
            env_name = srv['environment__name'] or '未分配'
            by_env[env_name] = by_env.get(env_name, 0) + 1
            project_name = srv['project_group__name'] or '未分配'
            by_project[project_name] = by_project.get(project_name, 0) + 1
        return ok({
            'total': qs.count(),
            'by_status': by_status,
            'by_env': by_env,
            'by_project': by_project,
        })

    @action(detail=False, methods=['get'])
    def export(self, request):
        """导出服务器 CSV：应用与列表页一致的筛选参数，列与导入模板一致（可回导）。"""
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = 'attachment; filename="servers.csv"'
        response.write('\ufeff')  # BOM：Excel 直接打开不乱码
        writer = csv.writer(response)
        writer.writerow(CSV_COLUMNS)
        for s in self.get_queryset():
            writer.writerow([
                s.name, s.private_ip, s.public_ip or '', s.os, s.cpu, s.memory, s.status,
                s.project_group.code if s.project_group else '',
                s.channel.code if s.channel else '',
                s.environment.code if s.environment else '',
                s.cloud_provider.code if s.cloud_provider else '',
                s.region, s.channel_mode, s.agent.name if s.agent else '',
                s.ssh_port, s.ssh_user, s.remark,
            ])
        return response


class ServerImportTemplateView(APIView):
    """下载服务器导入 CSV 模板：表头 + 一行示例，字典列填编码（code）。"""

    permission_classes = [IsAdminOrReadCreate]

    @extend_schema(responses=OpenApiTypes.BINARY)
    def get(self, request):
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = 'attachment; filename="servers_import_template.csv"'
        response.write('\ufeff')
        writer = csv.writer(response)
        writer.writerow(CSV_COLUMNS)
        writer.writerow([
            'web-server-01', '192.168.1.100', '', 'Ubuntu 22.04', '4', '8192', 'running',
            'legend', 'official', 'prod', 'aliyun',
            'cn-hangzhou', 'auto', '', '22', 'root',
            '示例行，导入前请删除；channel_mode 填 auto/direct/agent，填 agent 时 agent 列写 Agent 名称',
        ])
        return response


class ServerBatchUpdateView(APIView):
    """批量编辑服务器普通字段（状态/系统/地域/SSH 用户/SSH 端口/备注）。

    请求体::

        {"server_ids": [1, 2], "update": {"status": "maintenance", "ssh_port": 2222}}

    仅提交的字段会被更新；字典外键归属走「配置管理 → 服务器分配」批量分配。
    """

    # 批量改/删与导入同样要 sudo：一次能动一批主机的执行通道
    permission_classes = [IsAdmin, SudoRequired]

    @extend_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT)
    def post(self, request):
        server_ids = request.data.get('server_ids')
        update = request.data.get('update')
        if not isinstance(server_ids, list) or not server_ids:
            return fail('server_ids 必须为非空数组')
        if not isinstance(update, dict) or not update:
            return fail('update 必须为非空对象')
        unknown = set(update) - set(BATCH_UPDATE_FIELDS)
        if unknown:
            return fail(f'不支持批量编辑的字段：{", ".join(sorted(unknown))}')

        servers = Server.objects.filter(id__in=server_ids)
        if servers.count() != len(set(server_ids)):
            return fail('存在无效的服务器 id')

        fields = {}
        if 'status' in update:
            valid = {key for key, _ in Server.STATUS_CHOICES}
            if update['status'] not in valid:
                return fail(f'status 必须为 {" / ".join(sorted(valid))}')
            fields['status'] = update['status']
        if 'ssh_port' in update:
            port = update['ssh_port']
            if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
                return fail('ssh_port 必须为 1-65535 的整数')
            fields['ssh_port'] = port
        for key in ('os', 'region', 'ssh_user', 'remark'):
            if key in update:
                fields[key] = str(update[key] or '')
        if 'channel_mode' in update:
            mode = update['channel_mode']
            valid = {key for key, _ in Server.CHANNEL_MODE_CHOICES}
            if mode not in valid:
                return fail(f'channel_mode 必须为 {" / ".join(sorted(valid))}')
            fields['channel_mode'] = mode
            # 非「指定 Agent」模式清空钉死目标，避免留下不生效的残留配置
            if mode != 'agent':
                fields['agent'] = None
        if 'agent' in update:
            # 执行通道批量切换：null = 清空钉死目标（模式由 channel_mode 决定）
            agent_id = update['agent']
            if agent_id is None:
                fields['agent'] = None
            else:
                from apps.agent_mgr.models import AgentNode

                agent = AgentNode.objects.filter(pk=agent_id).first()
                if agent is None:
                    return fail('指定的 Agent 不存在')
                fields['agent'] = agent
        # 兼容只传 agent 的旧客户端：视为钉死该 agent
        if 'channel_mode' not in update and fields.get('agent') is not None:
            fields['channel_mode'] = 'agent'
        if fields.get('channel_mode') == 'agent' and fields.get('agent') is None:
            return fail('执行通道设为「指定 Agent」时必须同时指定 agent')

        with transaction.atomic():
            updated = servers.update(**fields, updated_at=timezone.now())
        return ok({'updated': updated})


class ServerBatchDeleteView(APIView):
    """批量删除服务器。存在「已部署」服务的主机禁止删除（先走下线流程）。

    请求体::

        {"server_ids": [1, 2, 3]}
    """

    # 批量改/删与导入同样要 sudo：一次能动一批主机的执行通道
    permission_classes = [IsAdmin, SudoRequired]

    @extend_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT)
    def post(self, request):
        server_ids = request.data.get('server_ids')
        if not isinstance(server_ids, list) or not server_ids:
            return fail('server_ids 必须为非空数组')
        servers = Server.objects.filter(id__in=server_ids)
        if servers.count() != len(set(server_ids)):
            return fail('存在无效的服务器 id')

        deployed = sorted(set(
            ServerService.objects
            .filter(server_id__in=server_ids, status='deployed')
            .values_list('server__name', flat=True)
        ))
        if deployed:
            names = '、'.join(deployed[:5]) + ('…' if len(deployed) > 5 else '')
            return fail(f'主机 {names} 存在「已部署」服务，请先在「服务器分配」标记下线后再删除')

        with transaction.atomic():
            deleted = servers.count()
            servers.delete()  # 服务记录 CASCADE 一并删除
        return ok({'deleted': deleted})


class ServerImportView(APIView):
    """CSV 批量导入服务器。

    multipart 表单::

        file: CSV 文件（模板见 servers/import-template/）
        mode: skip（默认，主机名已存在跳过）| update（主机名已存在则更新其余字段）

    更新目标按主机名匹配（name 全局唯一）。

    全量校验通过才写库（原子），任一行有错则整体不导入并返回逐行错误。
    """

    # 批量改/删与导入同样要 sudo：一次能动一批主机的执行通道
    permission_classes = [IsAdmin, SudoRequired]

    @extend_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT)
    def post(self, request):
        upload = request.FILES.get('file')
        mode = request.data.get('mode', 'skip')
        if upload is None:
            return fail('请上传 CSV 文件（表单字段 file）')
        if mode not in ('skip', 'update'):
            return fail('mode 必须为 skip 或 update')
        try:
            text = upload.read().decode('utf-8-sig')
        except UnicodeDecodeError:
            return fail('文件编码必须为 UTF-8（模板下载后请保持编码另存）')

        reader = csv.DictReader(io.StringIO(text))
        headers = [h.strip() for h in (reader.fieldnames or [])]
        missing = {'name', 'private_ip'} - set(headers)
        if missing:
            return fail(f'CSV 缺少必需列：{", ".join(sorted(missing))}（请使用导入模板）')

        # 字典缓存：code/name → 对象（导入按编码填写，兼容按名称填写）
        dict_cache = {
            key: {**{o.code: o for o in model.objects.all()},
                  **{o.name: o for o in model.objects.all()}}
            for key, (model, _) in CSV_DICT_FIELDS.items()
        }
        # 执行通道 Agent 缓存：CSV 的 agent 列填 Agent 名称（AgentNode.name 唯一）
        from apps.agent_mgr.models import AgentNode

        agent_cache = {a.name: a for a in AgentNode.objects.all()}
        # 主机名全局唯一，导入按 name 匹配更新目标
        existing_by_name = {s.name: s for s in Server.objects.all()}
        existing_public_ips = {
            s.public_ip: s.name for s in Server.objects.exclude(public_ip=None)
        }

        errors, to_create, to_update, skipped = [], [], [], 0
        seen_names, seen_public_ips = set(), set()
        for lineno, row in enumerate(reader, start=2):  # 第 1 行为表头
            parsed, row_errors = self._parse_row(row, dict_cache, agent_cache)
            if parsed is None and row_errors is None:
                continue  # 空行
            name = parsed.get('name')
            public_ip = parsed.get('public_ip')
            if name and name in seen_names:
                row_errors.append('文件内主机名重复')
            if name:
                seen_names.add(name)
            matched = existing_by_name.get(name)
            if public_ip:
                if public_ip in seen_public_ips:
                    row_errors.append(f'文件内公网 IP 重复：{public_ip}')
                seen_public_ips.add(public_ip)
                owner = existing_public_ips.get(public_ip)
                if owner and owner != name:
                    row_errors.append(f'公网 IP {public_ip} 已被主机「{owner}」使用')
            if row_errors:
                errors.append({'line': lineno, 'name': name or '', 'errors': row_errors})
                continue
            if matched:
                if mode == 'update':
                    to_update.append((matched, parsed))
                else:
                    skipped += 1
            else:
                to_create.append(Server(**parsed))
        if errors:
            return fail('导入校验未通过，已中止（未写入任何数据）', data={'errors': errors})

        with transaction.atomic():
            Server.objects.bulk_create(to_create)
            for server, parsed in to_update:
                for field, value in parsed.items():
                    setattr(server, field, value)
                server.save()
        return ok({'created': len(to_create), 'updated': len(to_update), 'skipped': skipped})

    @staticmethod
    def _parse_row(row, dict_cache, agent_cache):
        """解析单行，返回 (字段字典, 错误列表)；整行为空返回 (None, None)。"""
        get = lambda key: (row.get(key) or '').strip()  # noqa: E731
        if not any(get(key) for key in CSV_COLUMNS):
            return None, None
        errors = []
        parsed = {
            'name': get('name'),
            'private_ip': get('private_ip'),
            'public_ip': get('public_ip') or None,
        }
        if not parsed['name']:
            errors.append('name 不能为空')
        if not parsed['private_ip']:
            errors.append('private_ip 不能为空')
        else:
            try:
                validate_ipv46_address(parsed['private_ip'])
            except ValidationError:
                errors.append(f'private_ip 格式非法：{parsed["private_ip"]}')
        if parsed['public_ip']:
            try:
                validate_ipv46_address(parsed['public_ip'])
            except ValidationError:
                errors.append(f'public_ip 格式非法：{parsed["public_ip"]}')

        status = get('status') or 'running'
        valid_status = {key for key, _ in Server.STATUS_CHOICES}
        if status not in valid_status:
            errors.append(f'status 非法：{status}（可选 {"/".join(sorted(valid_status))}）')
        parsed['status'] = status

        for key, default in (('cpu', 0), ('memory', 0), ('ssh_port', 22)):
            raw = get(key)
            if not raw:
                parsed[key] = default
                continue
            try:
                value = int(raw)
                if value < 0 or (key == 'ssh_port' and not 1 <= value <= 65535):
                    raise ValueError
                parsed[key] = value
            except ValueError:
                errors.append(f'{key} 必须为合法整数：{raw}')

        for key, (_, label) in CSV_DICT_FIELDS.items():
            raw = get(key)
            if not raw:
                parsed[key] = None
                continue
            obj = dict_cache[key].get(raw)
            if obj is None:
                errors.append(f'{label}不存在：{raw}（填字典编码，可在对应字典页查看）')
            else:
                parsed[key] = obj

        # 执行通道：channel_mode 填模式码（兼容中文标签），agent 填 Agent 名称。
        # 两列都不在文件里时整体跳过，避免 update 模式用缺列的 CSV 把已钉死的通道重置为 auto。
        if 'channel_mode' in row or 'agent' in row:
            mode_by_label = {label: key for key, label in Server.CHANNEL_MODE_CHOICES}
            raw_mode = get('channel_mode')
            mode = mode_by_label.get(raw_mode, raw_mode) or 'auto'
            valid_modes = {key for key, _ in Server.CHANNEL_MODE_CHOICES}
            if mode not in valid_modes:
                errors.append(
                    f'channel_mode 非法：{raw_mode}（可选 {"/".join(sorted(valid_modes))}）')
                mode = 'auto'
            raw_agent = get('agent')
            agent = None
            if raw_agent:
                agent = agent_cache.get(raw_agent)
                if agent is None:
                    errors.append(f'Agent 不存在：{raw_agent}（填 Agent 名称，见「执行代理」页）')
            if mode == 'agent' and not raw_agent:
                errors.append('channel_mode=agent 时必须在 agent 列填写 Agent 名称')
            parsed['channel_mode'] = mode
            # 非「指定 Agent」模式清空钉死目标，与批量编辑口径一致
            parsed['agent'] = agent if mode == 'agent' else None

        parsed['os'] = get('os')
        parsed['region'] = get('region')
        parsed['ssh_user'] = get('ssh_user') or 'root'
        parsed['remark'] = get('remark')
        return parsed, errors


class ServerRegisterJumpServerView(APIView):
    """把选中服务器注册到 JumpServer（幂等：已存在的资产跳过）。

    请求体::

        {"server_ids": [1, 2, 3]}

    连接信息读系统配置 jumpserver.url / jumpserver.token（详见 common.jumpserver）。
    """

    permission_classes = [IsAdmin]

    @extend_schema(request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT)
    def post(self, request):
        server_ids = request.data.get('server_ids')
        if not isinstance(server_ids, list) or not server_ids:
            return fail('server_ids 必须为非空数组')
        servers = list(Server.objects.filter(id__in=server_ids))
        if len(servers) != len(set(server_ids)):
            return fail('存在无效的服务器 id')
        try:
            results = register_servers(servers)
        except ValueError as exc:
            return fail(str(exc))
        succeeded = sum(1 for r in results if r['ok'])
        return ok({
            'total': len(results),
            'succeeded': succeeded,
            'failed': len(results) - succeeded,
            'results': results,
        })
