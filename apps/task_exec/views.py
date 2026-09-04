import logging
import os
from pathlib import Path

import yaml

from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.views import APIView

from apps.auth_mgr.apikey import (
    ApiKeyAuthentication,
    ApiKeyThrottle,
    request_credential,
    user_or_scope,
)
from apps.auth_mgr import roles
from apps.auth_mgr.authentication import MfaJWTAuthentication
from common.ansible import resolve_within
from common.crypto import DecryptError
from apps.auth_mgr.permissions import module_perm
from common import sudo
from common.response import fail, ok
from common.sudo import SudoRequired

from .models import Credential, ExecutionTask, ExecVariable
from .serializers import (
    CredentialSerializer,
    ExecutionTaskListSerializer,
    ExecutionTaskSerializer,
    ExecVariableSerializer,
    ExecVariableVersionSerializer,
    TaskExecutionLogDetailSerializer,
    TaskExecutionLogSerializer,
)
from . import trigger


logger = logging.getLogger('apps.task_exec')


def _exec_permission_error(user, task):
    """发起/处理任务执行的权限校验，返回中文错误消息；None 表示放行。

    三道：能力位（可否发起执行）→ 生产环境（可否碰生产）→ 数据范围（能碰哪些项目）。

    生产单独立一个能力位而不是塞进模块矩阵：「能改任务定义」与「能往生产推」是两件
    事，前者错了可以改回来，后者错了已经推上去了。这也是最高频的真实诉求。
    """
    if not roles.user_capability(user, 'can_execute'):
        return '当前角色没有发起任务执行的权限。'
    environment = task.environment
    if environment is not None and environment.is_prod \
            and not roles.user_capability(user, 'can_exec_prod'):
        return f'当前角色不允许对生产环境（{environment.name}）发起执行。'
    if not user.allows_dims(task.project_group_id, task.environment_id):
        return f'该任务不在你的数据范围内（当前范围：{user.scope_label}）。'
    return None


def _secret_keys(dims):
    """该维度下被排除的敏感变量名（resolved 接口告知外部系统）。"""
    return list(
        ExecVariable.objects.filter(
            enabled=True, is_secret=True,
            project_group_id=dims['project_group'].id,
            channel_id=dims['channel'].id,
            environment_id=dims['environment'].id,
        ).order_by('key').values_list('key', flat=True)
    )


# 剧本扫描时跳过的目录：Ansible 工程的非剧本组成部分
PLAYBOOK_EXCLUDE_DIRS = {
    'roles', 'inventories', 'inventory', 'group_vars', 'host_vars',
    'collections', 'files', 'templates', 'vars', 'library', 'filter_plugins',
}


class PlaybookListView(APIView):
    """列出 ANSIBLE_PROJECT_DIR 内可选的剧本文件（相对路径，供任务表单下拉）。

    两处都用得到：定义 ansible 方法（method 模块）与在任务流里加一个 playbook
    模块（exec 模块）。所以是「两个写档给一个就行」的 OR，而不是挂在其中一个
    模块上——挂谁都会让另一边的编辑页少一个下拉。
    """

    permission_classes = [
        module_perm('method', min_level='write') | module_perm('exec', min_level='write'),
    ]

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request):
        project_dir = getattr(settings, 'ANSIBLE_PROJECT_DIR', '')
        if not project_dir or not os.path.isdir(project_dir):
            return fail(f'Ansible 项目目录不存在：{project_dir!r}（请配置 ANSIBLE_PROJECT_DIR）')
        playbooks = []
        for root, dirs, files in os.walk(project_dir):
            # 原地过滤，阻止 os.walk 深入非剧本目录与隐藏目录
            dirs[:] = [d for d in dirs if d not in PLAYBOOK_EXCLUDE_DIRS and not d.startswith('.')]
            for filename in sorted(files):
                if filename.endswith(('.yml', '.yaml')) and not filename.startswith('.'):
                    playbooks.append(os.path.relpath(os.path.join(root, filename), project_dir))
        return ok({'results': sorted(playbooks)})


class CredentialViewSet(viewsets.ModelViewSet):
    """连接凭据管理（仅管理员）。"""

    queryset = Credential.objects.all()
    serializer_class = CredentialSerializer
    # 配置面写操作要求 sudo 二次验证（common/sudo.py）：改了就能拿到更多东西，
    # 而登录那次 MFA 可能是 60 分钟前的事
    # 按方法推导档位：凭据的**读**只暴露名称/登录用户/认证方式（密码与私钥是
    # write_only，只回 has_xxx 布尔），给「运维管理」只读是有意义的——能在任务里
    # 选凭据、看得到有哪些，但改不了。改/删仍需 write + sudo
    permission_classes = [module_perm('credential'), SudoRequired]


class ExecVariableViewSet(viewsets.ModelViewSet):
    """执行变量管理：按任务/模块有效维度加载注入 --extra-vars；亦供外部系统拉取。

    普通用户只读，管理员可维护；每次保存/回滚落版本快照（默认保留 10 版）。
    """

    queryset = ExecVariable.objects.select_related('project_group', 'channel', 'environment')
    serializer_class = ExecVariableSerializer
    permission_classes = [module_perm('variable')]

    # 查询参数 → 维度外键映射（纯数字按 id、否则按 code，与服务器接口一致）
    DICT_FILTERS = (
        ('project_group', 'project_group'),
        ('channel', 'channel'),
        ('env', 'environment'),
    )

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        enabled = params.get('enabled')
        keyword = params.get('keyword')
        if enabled in ('true', 'false'):
            qs = qs.filter(enabled=(enabled == 'true'))
        for param, field in self.DICT_FILTERS:
            value = params.get(param)
            if value:
                if value.isdigit():
                    qs = qs.filter(**{f'{field}_id': value})
                else:
                    qs = qs.filter(**{f'{field}__code': value})
        if keyword:
            from django.db.models import Q
            qs = qs.filter(Q(key__icontains=keyword) | Q(description__icontains=keyword))
        return qs

    @extend_schema(
        parameters=[
            OpenApiParameter('project_group', str, description='项目组（id 或 code），必填'),
            OpenApiParameter('channel', str, description='渠道（id 或 code），必填'),
            OpenApiParameter('env', str, description='环境（id 或 code），必填'),
            OpenApiParameter('output', str, description='json（默认）| yaml，yaml 直接返回纯文本'),
            OpenApiParameter('include_artifact', str, description='true 时附加 package_* 制品变量'),
            OpenApiParameter('artifact_version', str, description='锁定制品版本，留空取最新'),
        ],
        responses=OpenApiTypes.OBJECT,
    )
    @action(
        detail=False, methods=['get'],
        # 只在这个动作上开机读通道：脚本要的是「按维度解析好的变量字典」，
        # 不是整张变量表，把 list 也开出去只是白扩大暴露面
        authentication_classes=[ApiKeyAuthentication, MfaJWTAuthentication],
        permission_classes=[user_or_scope(module_perm('variable'), 'variable:read')],
        throttle_classes=[ApiKeyThrottle],
    )
    def resolved(self, request):
        """按维度返回已解析合并的变量字典，供外部系统直接喂给 ansible。

        与执行器共用 runner.collect_dim_variables，语义完全一致（一条变量记录
        = 一个 ansible 变量，值为 YAML 解析结果）。三维度必填，避免精确匹配语义
        含糊；output=yaml 时返回不带 {code,msg,data} 包装的纯 YAML，可直接
        `curl -o vars.yml` 后 `ansible-playbook -e @vars.yml`（用 output 而非
        format，后者被 DRF 内容协商占用）。
        """
        from .runner import ArtifactLookupError, collect_artifact_vars, collect_dim_variables

        dims, err = self._resolve_dims(request.query_params)
        if err:
            return fail(err)
        # 机读凭证：请求的维度必须在凭证数据范围内。这里是拒绝而不是收窄——
        # 三维度是必填参数，调用方已经明确说了要哪一组，静默换成别的组更糟
        credential = request_credential(request)
        if credential is not None and not credential.allows_dims(
                dims['project_group'].id, dims['environment'].id):
            return fail(f'凭证「{credential.name}」的数据范围不含该维度'
                        f'（当前范围：{credential.scope_label}）', status=403)
        # 登录用户的数据范围同一套口径（common/dimscope）：变量里可能有密码，
        # 不该让某个项目的人拉到别的项目的变量
        if credential is None and not request.user.allows_dims(
                dims['project_group'].id, dims['environment'].id):
            return fail(f'你的数据范围不含该维度（当前范围：{request.user.scope_label}）',
                        status=403)

        # 敏感变量默认不下发：这个接口只读档也能调，全量返回等于任何账号拉一次就把
        # 密码全拿走。要真值必须显式 include_secrets=true，且：
        # 登录用户须有 variable 写档（与 variables/{id}/reveal/ 同一道口径，
        # 那里也是写档 —— 看真值是同一件事，不该因为换个接口就换成另一套判定）；
        # 机读凭证须另有 variable:secret 作用域。
        # 这里刻意不再判 is_staff：角色矩阵落地后 is_staff 只是 Django 后台入口与
        # 无角色时的回退依据，拿它当权限判据会让「配了 variable=write 的非 staff
        # 角色」在 reveal 能看、在这里被拒
        include_secrets = request.query_params.get('include_secrets') == 'true'
        if include_secrets:
            if credential is not None:
                if not credential.has_scope('variable:secret'):
                    return fail(f'凭证「{credential.name}」缺少作用域：variable:secret',
                                status=403)
            elif not roles.user_can(request.user, 'variable', 'PUT'):
                return fail('include_secrets 需要「执行变量」模块的读写权限', status=403)
            elif not sudo.is_active(request.user.pk, sudo.token_jti(request)):
                # 与 reveal 对齐：那里看**一个**变量的真值都要 sudo，这里一次拉回
                # 该维度下**全部**密码类变量，暴露面只会更大。SudoRequired 权限类
                # 放行所有 GET，管不到这个查询参数，所以在这里显式判一次
                raise sudo.SudoRequiredError()
        variables = collect_dim_variables(dims['project_group'].id, dims['channel'].id,
                                          dims['environment'].id,
                                          include_secrets=include_secrets)
        excluded = [] if include_secrets else _secret_keys(dims)
        if request.query_params.get('include_artifact') == 'true':
            try:
                variables.update(collect_artifact_vars(
                    dims['project_group'].id, dims['channel'].id, dims['environment'].id,
                    locked=request.query_params.get('artifact_version', ''),
                ))
            except ArtifactLookupError as exc:
                return fail(str(exc))
        if request.query_params.get('output') == 'yaml':
            text = yaml.safe_dump(variables, allow_unicode=True, default_flow_style=False)
            return HttpResponse(text, content_type='application/x-yaml; charset=utf-8')
        return ok({
            'dims': {name: {'id': obj.id, 'code': obj.code, 'name': obj.name}
                     for name, obj in dims.items()},
            'count': len(variables),
            'vars': variables,
            # 告知被排除的敏感变量名：外部系统据此知道要自己补，而不是拿着
            # 缺变量的字典去跑然后在剧本里报 undefined
            'secret_keys_excluded': excluded,
        })

    @staticmethod
    def _resolve_dims(params):
        """解析三维度查询参数为字典对象，返回 (dims, 错误信息)。纯数字按 id、否则按 code。"""
        from apps.asset_mgr.models import Channel, Environment, ProjectGroup

        specs = (
            ('project_group', 'project_group', ProjectGroup, '项目组'),
            ('channel', 'channel', Channel, '渠道'),
            ('environment', 'env', Environment, '环境'),
        )
        dims = {}
        for field, param, model, label in specs:
            value = (params.get(param) or '').strip()
            if not value:
                return None, f'缺少查询参数 {param}（{label}），三个维度均为必填'
            lookup = {'id': value} if value.isdigit() else {'code': value}
            obj = model.objects.filter(**lookup).first()
            if obj is None:
                return None, f'{label}不存在：{param}={value}'
            dims[field] = obj
        return dims, None

    @extend_schema(request=None, responses=OpenApiTypes.OBJECT)
    @action(detail=True, methods=['post'], permission_classes=[module_perm('variable', min_level='write'), SudoRequired])
    def reveal(self, request, pk=None):
        """查看敏感变量真值（管理员 + sudo 二次验证）。

        运维现实里确实需要能核对密码，所以不做成「写进去就再也看不到」。
        但要过 sudo、且这条 POST 会进审计表（谁在什么时候看了哪个变量）——
        用 POST 而非 GET 正是为了让它进审计的写操作记录。
        """
        variable = self.get_object()
        try:
            value = variable.get_value()
        except DecryptError as exc:
            return fail(f'变量值无法解密：{exc}')
        logger.warning('管理员 %s 查看了敏感变量 %s 的真值',
                       request.user.username, variable.key)
        return ok({'key': variable.key, 'value': value})

    @action(detail=True, methods=['get'])
    def versions(self, request, pk=None):
        """历史版本列表（新→旧，最多保留 EXEC_VARIABLE_VERSION_LIMIT 版）。"""
        variable = self.get_object()
        qs = variable.versions.select_related('created_by', 'project_group', 'channel', 'environment')
        return ok(ExecVariableVersionSerializer(qs, many=True).data)

    @action(detail=True, methods=['post'], permission_classes=[module_perm('variable', min_level='write')])
    def rollback(self, request, pk=None):
        """回滚到指定历史版本：套用快照内容并生成一条新版本记录。"""
        from .models import snapshot_variable

        variable = self.get_object()
        version_no = request.data.get('version')
        version = variable.versions.filter(version=version_no).first()
        if version is None:
            return fail(f'版本 {version_no} 不存在（超出保留范围或参数有误）')
        # 原样搬存储值与敏感标记：快照里 is_secret 的值本就是密文，
        # 只搬 value 不搬 is_secret 会得到「标记说明文、内容是密文」的变量
        variable.value = version.value
        variable.is_secret = version.is_secret
        variable.description = version.description
        variable.enabled = version.enabled
        # 维度快照为弱关联：字典项已被删除的维度保持当前值不动
        for field in ('project_group_id', 'channel_id', 'environment_id'):
            snap = getattr(version, field)
            if snap is not None:
                setattr(variable, field, snap)
        variable.save()
        snapshot_variable(variable, user=request.user)
        return ok(ExecVariableSerializer(variable).data, msg=f'已回滚到版本 {version_no}')


class ExecutionTaskViewSet(viewsets.ModelViewSet):
    """任务模块：新建/编辑仅保存定义，执行走 execute 动作（主机按维度实时匹配）。

    普通用户只读，管理员可保存/编辑/执行。
    """

    queryset = (
        ExecutionTask.objects
        .select_related('credential', 'created_by', 'project_group', 'channel', 'environment')
        .prefetch_related(
            'servers',
            'steps__method', 'steps__project_group', 'steps__channel', 'steps__environment',
        )
    )
    serializer_class = ExecutionTaskSerializer
    permission_classes = [module_perm('exec')]
    http_method_names = ['get', 'post', 'put', 'patch', 'delete', 'head', 'options']

    def get_serializer_class(self):
        # 列表去掉 output（每行最多 6 万字符的日志预览，列表不展示它）
        if self.action == 'list':
            return ExecutionTaskListSerializer
        return super().get_serializer_class()

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        task_type = params.get('task_type')
        status = params.get('status')
        keyword = params.get('keyword')
        if task_type:
            # 类型下沉到模块后，筛选语义 = 包含该类型模块的任务
            qs = qs.filter(steps__task_type=task_type).distinct()
        if status:
            qs = qs.filter(status=status)
        if keyword:
            qs = qs.filter(name__icontains=keyword)
        return qs

    def perform_create(self, serializer):
        # 仅保存任务定义，不触发执行
        serializer.save(created_by=self.request.user)

    def update(self, request, *args, **kwargs):
        if self.get_object().status in ('running', 'paused'):
            return fail('任务正在执行中，无法编辑')
        return super().update(request, *args, **kwargs)

    @action(detail=True, methods=['get'])
    def matched_servers(self, request, pk=None):
        """执行表单数据源：全局范围匹配主机 + 各模块的主机来源与自定义匹配结果。"""
        from apps.asset_mgr.serializers import ServerSerializer

        task = self.get_object()
        global_servers = list(task.matched_servers())
        steps = []
        for step in task.steps.select_related('service_type'):
            entry = {
                'order': step.order, 'name': step.name, 'task_type': step.task_type,
                'task_type_display': step.get_task_type_display(),
                'custom': step.use_custom_scope,
                'service_type': step.service_type_id,
                'service_type_name': step.service_type.name if step.service_type_id else None,
            }
            if step.use_custom_scope:
                servers = list(step.matched_servers())
                entry['total'] = len(servers)
                entry['servers'] = ServerSerializer(servers, many=True).data
            else:
                # 跟随全局：服务类型过滤后的全局主机 id 由前端与勾选取交集实时算台数
                servers = step.resolve_servers(global_servers)
                entry['total'] = len(servers)
                if step.service_type_id:
                    entry['server_ids'] = [s.id for s in servers]
            steps.append(entry)
        return ok({
            'global': {'results': ServerSerializer(global_servers, many=True).data, 'total': len(global_servers)},
            'steps': steps,
        })

    @action(
        detail=True, methods=['post'],
        # 「发起执行」不该按 POST 推导成 read_create 档：执行不是改任务定义。
        # 「普通运维」的设计正是 exec 只读 + 可执行 —— 能跑不能改。
        # 能力位、生产环境与数据范围三道由 _exec_permission_error 判，
        # 它给的是具体原因（哪一道没过），比权限类的通用 403 好排查。
        permission_classes=[module_perm('exec', min_level='read')],
    )
    def execute(self, request, pk=None):
        """执行任务流。body.step_servers={模块id: [主机id]} 逐模块勾选（跟随全局的
        模块各自独立），body.server_ids 为未单独勾选模块的兜底（缺省=全部全局匹配
        主机，任务关闭 default_select_all 时缺省会被拒绝）；
        自定义范围模块按其维度全量执行。逐模块校验主机非空与方法服务规划。"""
        task = self.get_object()
        error = _exec_permission_error(request.user, task)
        if error:
            return fail(error, status=403)
        error = trigger.preflight(task)
        if error:
            return fail(error)
        steps = list(task.steps.all())

        # 全局范围主机：维度匹配 ∩ 勾选子集
        matched = {s.id: s for s in task.matched_servers()}
        step_ids = {s.id for s in steps}
        step_servers = request.data.get('step_servers') or {}
        if not isinstance(step_servers, dict):
            return fail('step_servers 必须为 {模块id: [主机id]} 对象')
        # 逐模块勾选：键为模块 id，值为主机 id 数组，主机必须在全局匹配集内
        picked = {}
        for raw_step_id, ids in step_servers.items():
            try:
                step_id = int(raw_step_id)
            except (TypeError, ValueError):
                return fail(f'step_servers 的模块 id「{raw_step_id}」非法')
            if step_id not in step_ids:
                return fail(f'step_servers 含不属于本任务的模块 id {step_id}，请刷新执行页面后重试')
            if not isinstance(ids, list):
                return fail(f'step_servers[{step_id}] 必须为主机 id 数组')
            invalid = [i for i in ids if i not in matched]
            if invalid:
                return fail(f'主机 id {invalid} 不在全局范围匹配集内，请刷新执行页面后重试')
            picked[step_id] = [matched[i] for i in ids]

        server_ids = request.data.get('server_ids')
        if server_ids is None:
            # 关闭默认全选的任务：每个跟随全局的模块都必须由发起人逐台确认
            if not task.default_select_all:
                unpicked = [
                    s.name for s in steps
                    if s.task_type != 'pause' and not s.use_custom_scope and s.id not in picked
                ]
                if unpicked:
                    return fail(
                        f'该任务已关闭「默认全选主机」，模块 {"、".join(unpicked)} 需在执行表单中人工勾选目标主机'
                    )
            selected_global = list(matched.values())
        else:
            if not isinstance(server_ids, list):
                return fail('server_ids 必须为主机 id 数组（缺省表示全部全局匹配主机）')
            invalid = [i for i in server_ids if i not in matched]
            if invalid:
                return fail(f'主机 id {invalid} 不在全局范围匹配集内，请刷新执行页面后重试')
            selected_global = [matched[i] for i in server_ids]

        log, error = trigger.launch(
            task, steps, selected_global, picked,
            triggered_by=request.user, trigger_type='execute',
        )
        if error:
            return fail(error)
        return ok(
            {'task': ExecutionTaskSerializer(task).data, 'log_id': log.id, 'step_count': len(steps)},
            msg=f'已发起执行（{len(steps)} 个模块，{task.get_exec_mode_display()}）',
        )

    # 暂停类型 -> 允许的处理动作（'' 兼容存量：视为暂停确认）
    PAUSE_ACTIONS = {
        'confirm': ('continue', 'abort'),
        'failed': ('retry', 'skip', 'abort'),
    }

    @action(
        detail=True, methods=['post'],
        # 与 execute 同一道口径：处理暂停会让剩余模块真的跑起来，属「执行」而非
        # 「改定义」，用能力位判而不是要求 exec 写权限
        permission_classes=[module_perm('exec', min_level='read')],
    )
    def confirm(self, request, pk=None):
        """人工处理暂停中的任务流。

        与发起执行同一道能力位与范围校验：continue / retry / skip 都会让剩余模块
        真的跑起来，只在 execute 上拦等于留了个后门。

        暂停确认模块（pause_type=confirm）：continue 继续 / abort 中止；
        模块失败等待（pause_type=failed）：retry 重试失败模块 / skip 跳过
        继续后续模块 / abort 中止。仅 paused 状态可操作；动作写入
        pause_action 由 runner 轮询消费。
        """
        task = self.get_object()
        error = _exec_permission_error(request.user, task)
        if error:
            return fail(error, status=403)
        action_value = request.data.get('action')
        if action_value not in ('continue', 'abort', 'retry', 'skip'):
            return fail('action 必须为 continue/abort（暂停确认）或 retry/skip/abort（失败处理）')
        from .models import ExecutionTask

        row = ExecutionTask.objects.filter(pk=pk).values_list('status', 'pause_type').first()
        if row is None or row[0] != 'paused':
            return fail('任务当前不在「等待处理」状态，无法操作')
        allowed = self.PAUSE_ACTIONS[row[1] or 'confirm']
        if action_value not in allowed:
            return fail(f'当前暂停类型仅支持动作：{"/".join(allowed)}')
        updated = ExecutionTask.objects.filter(pk=pk, status='paused').update(pause_action=action_value)
        if not updated:
            return fail('任务当前不在「等待处理」状态，无法操作')
        return ok({'action': action_value})

    @action(detail=True, methods=['get'])
    def output(self, request, pk=None):
        """轻量轮询接口：仅返回状态与输出（支持增量拉取）。

        带 ?offset=N 时只回传 N 之后的新增字符（delta=true），避免执行中
        每秒重传整段日志；输出已达预览上限（尾部截断，前缀会漂移）或 offset
        越界时回退整段（delta=false），前端按 delta 决定追加还是替换。
        """
        from .live import PREVIEW_LIMIT

        task = self.get_object()
        out = task.output or ''
        try:
            offset = max(0, int(request.query_params.get('offset') or 0))
        except (TypeError, ValueError):
            offset = 0
        delta = 0 < offset <= len(out) and len(out) < PREVIEW_LIMIT
        return ok({
            'id': task.id,
            'status': task.status,
            'pause_type': task.pause_type,
            'return_code': task.return_code,
            'output': out[offset:] if delta else out,
            'offset': len(out),
            'delta': delta,
            'duration': task.duration,
        })

    @action(detail=True, methods=['get'])
    def logs(self, request, pk=None):
        """任务执行历史列表（每次执行一条日志，不含输出大字段）。"""
        task = self.get_object()
        qs = task.logs.select_related('triggered_by')
        return ok(TaskExecutionLogSerializer(qs, many=True).data)

    @action(detail=True, methods=['get'], url_path=r'logs/(?P<log_id>\d+)')
    def log_detail(self, request, pk=None, log_id=None):
        """单条执行日志详情，含完整输出（超长部分从落盘文件读取）。"""
        task = self.get_object()
        log = get_object_or_404(task.logs.select_related('triggered_by'), pk=log_id)
        return ok(TaskExecutionLogDetailSerializer(log).data)

    # 执行现场文件的单次读取上限（字符），超出截断——现场文件本应很小，
    # 超限通常意味着变量表异常膨胀，截断即可，不必把大字符串塞给前端
    SCENE_READ_LIMIT = 200000

    @extend_schema(
        parameters=[OpenApiParameter('file', str, description='归档内相对路径，缺省则列出清单')],
        responses=OpenApiTypes.OBJECT,
    )
    # 现场归档与「日志文件」页读的是同一批文件（TASK_LOG_DIR/<task>/<log>.d/），
    # 归档时敏感变量已打码、inventory 不含凭据（见 runner.archive_scene），所以
    # 与执行日志同档（exec read）。把这里单独抬到 write 只会造成「同一个文件，
    # 换个入口就能看」的假门槛
    @action(detail=True, methods=['get'], url_path=r'logs/(?P<log_id>\d+)/scene',
            permission_classes=[module_perm('exec', min_level='read')])
    def log_scene(self, request, pk=None, log_id=None):
        """执行现场归档（inventory / 变量 / 命令）：缺省列清单，带 file 返回内容。

        归档含变量值，仅管理员可读；file 经 resolve_within 限制在本条执行记录
        的归档目录内，防路径穿越。文件过保留期被清理后清单为空。
        """
        task = self.get_object()
        log = get_object_or_404(task.logs, pk=log_id)
        root = Path(settings.TASK_LOG_DIR) / str(task.pk) / f'{log.pk}.d'
        rel = (request.query_params.get('file') or '').strip()
        if not rel:
            files = sorted(
                ({'path': str(p.relative_to(root)), 'size': p.stat().st_size}
                 for p in root.rglob('*') if p.is_file()),
                key=lambda item: item['path'],
            ) if root.is_dir() else []
            return ok({'files': files, 'retained': bool(files)})
        try:
            # 报错文案面向本接口，resolve_within 的默认文案是 Ansible 项目目录
            path = Path(resolve_within(str(root), rel))
        except ValueError:
            return fail(f'执行现场文件路径非法：{rel}')
        if not path.is_file():
            return fail(f'执行现场文件不存在或已过保留期：{rel}')
        content = path.read_text(encoding='utf-8', errors='replace')
        truncated = len(content) > self.SCENE_READ_LIMIT
        return ok({
            'path': rel,
            'content': content[:self.SCENE_READ_LIMIT],
            'truncated': truncated,
        })
