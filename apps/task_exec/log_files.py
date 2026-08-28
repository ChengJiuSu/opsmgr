"""任务日志文件浏览（只读文件系统视图，`exec` 模块 read 档即可）。

以 ``settings.TASK_LOG_DIR`` 为根，把落盘的执行日志与执行现场归档整棵目录树
开放给 Web 页面按目录浏览：

    TASK_LOG_DIR/<task_id>/<log_id>.log              完整执行日志
    TASK_LOG_DIR/<task_id>/<log_id>.d/step<n>/<通道>/ 执行现场归档
        ├── inventory.ini   主机文件（无密钥版本）
        ├── vars.yml        变量文件
        └── command.txt     实际执行命令（可复制重跑）

与 ExecutionTaskViewSet.log_scene 的区别：那个接口按「某任务的某次执行」定向取
归档，本模块是不依赖数据库记录的目录浏览器——记录已被清理、或任务已删除但文件
仍在时，仍能翻到文件。两者共用同一片目录，权限也一致（`exec` read）。

**为什么只读档就够**：日志正文与 `logs/{id}/` 详情接口返回的是同一份内容（超长
部分本就从这些文件读），归档里的敏感变量在落盘时已打码、inventory 是无密钥版本
（见 runner.archive_scene）。要求写档拦不住任何东西，只是把「看自己跑过的日志」
变成管理员专属——新人排障恰恰最需要翻这些。

安全：所有入参路径经 resolve_within 收敛到 TASK_LOG_DIR 内（realpath 比较，
软链接指向外部同样拒绝），只读，不提供任何写/删接口。
"""
import os
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

from django.conf import settings
from django.http import FileResponse
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.views import APIView

from common.ansible import resolve_within
from apps.auth_mgr.permissions import module_perm
from common.response import fail, ok

# 单个目录返回的条目上限（日志目录条目本就不多，纯属兜底防御）
MAX_ENTRIES = 2000
# 文本内容单次返回的字节上限（默认值同时也是最大值，超出请下载原文件）
CONTENT_LIMIT = 512 * 1024
# 归档目录后缀（<log_id>.d），列表里标注为「执行现场」
SCENE_DIR_SUFFIX = '.d'


def log_root():
    """日志根目录（Path）。目录可能尚未创建，调用方需自行判断存在性。"""
    return Path(settings.TASK_LOG_DIR)


def resolve_log_path(rel):
    """把相对路径解析为根目录内的绝对路径，返回 (Path, 错误信息)。

    根目录本身用空串表示；越界（含 ../ 与指向外部的软链接）返回中文错误。
    """
    rel = (rel or '').strip().lstrip('/')
    try:
        return Path(resolve_within(str(log_root()), rel)), None
    except ValueError:
        return None, f'日志文件路径非法：{rel}'


def _entry_sort_key(entry):
    """名字以数字打头（任务 id / 执行记录 id）的排在前且按数值倒序，新的在前；
    同一编号的归档目录与日志文件相邻（<id>.d 在 <id>.log 前），其余目录优先。"""
    name = entry['name']
    stem = name.split('.')[0]
    numeric = stem.isdigit()
    return (0 if numeric else 1, -int(stem) if numeric else 0,
            0 if entry['type'] == 'dir' else 1, name)


def _modified_at(stat):
    """mtime → 本地时区 ISO 字符串（USE_TZ=True，统一走 django 时区转换）。"""
    return timezone.localtime(
        datetime.fromtimestamp(stat.st_mtime, tz=dt_timezone.utc)).isoformat(timespec='seconds')


def list_dir(path, rel):
    """列出目录条目（不递归），返回按 _entry_sort_key 排序的列表。"""
    entries = []
    with os.scandir(path) as it:
        for item in it:
            try:
                stat = item.stat()
            except OSError:  # 扫描期间被清理任务删掉，跳过即可
                continue
            is_dir = item.is_dir()
            entries.append({
                'name': item.name,
                'path': f'{rel}/{item.name}' if rel else item.name,
                'type': 'dir' if is_dir else 'file',
                'size': None if is_dir else stat.st_size,
                'modified_at': _modified_at(stat),
                'label': '',
            })
            if len(entries) >= MAX_ENTRIES:
                break
    entries.sort(key=_entry_sort_key)
    return entries


def _annotate(rel, entries):
    """给条目补业务标注：根层目录=任务名，任务目录内=该次执行的状态与时间。

    标注纯属可读性增强，数据库记录已删（过保留期清理）时留空，不影响浏览。
    """
    parts = [p for p in rel.split('/') if p]
    if not parts:
        _annotate_tasks(entries)
    elif len(parts) == 1 and parts[0].isdigit():
        _annotate_logs(int(parts[0]), entries)
    return entries


def _annotate_tasks(entries):
    from .models import ExecutionTask

    ids = [int(e['name']) for e in entries if e['name'].isdigit()]
    names = dict(ExecutionTask.objects.filter(id__in=ids).values_list('id', 'name'))
    for entry in entries:
        if entry['name'].isdigit():
            entry['label'] = names.get(int(entry['name']), '任务已删除')


def _annotate_logs(task_id, entries):
    from .models import TaskExecutionLog

    stems = {e['name'].split('.')[0] for e in entries}
    ids = [int(s) for s in stems if s.isdigit()]
    logs = {
        log.pk: log for log in
        TaskExecutionLog.objects.filter(task_id=task_id, id__in=ids)
    }
    for entry in entries:
        stem = entry['name'].split('.')[0]
        log = logs.get(int(stem)) if stem.isdigit() else None
        kind = '执行现场' if entry['name'].endswith(SCENE_DIR_SUFFIX) else '执行日志'
        if log is None:
            entry['label'] = f'{kind}（记录已删除）'
            continue
        started = timezone.localtime(log.started_at).strftime('%Y-%m-%d %H:%M:%S') \
            if log.started_at else '-'
        entry['label'] = f'{kind} · {log.get_status_display()} · {started}'


class TaskLogFileListView(APIView):
    """任务日志目录浏览：列出 TASK_LOG_DIR 内某层目录的子目录与文件。"""

    permission_classes = [module_perm('exec')]

    @extend_schema(
        parameters=[OpenApiParameter('path', str, description='相对 TASK_LOG_DIR 的目录路径，缺省为根')],
        responses=OpenApiTypes.OBJECT,
    )
    def get(self, request):
        rel = (request.query_params.get('path') or '').strip().strip('/')
        path, err = resolve_log_path(rel)
        if err:
            return fail(err)
        if not path.is_dir():
            # 根目录尚未生成（从未执行过任务）时给空列表而非报错
            if not rel:
                return ok({'path': '', 'parent': None, 'entries': [], 'root': str(log_root())})
            return fail(f'目录不存在或已过保留期：{rel}')
        entries = _annotate(rel, list_dir(path, rel))
        return ok({
            'path': rel,
            'parent': None if not rel else rel.rsplit('/', 1)[0] if '/' in rel else '',
            'entries': entries,
            'total': len(entries),
            'root': str(log_root()),
        })


class TaskLogFileContentView(APIView):
    """任务日志文件内容：按字节区间返回文本（大日志分段读，或直接读末尾）。"""

    permission_classes = [module_perm('exec')]

    @extend_schema(
        parameters=[
            OpenApiParameter('path', str, description='相对 TASK_LOG_DIR 的文件路径，必填'),
            OpenApiParameter('offset', int, description='起始字节偏移，默认 0'),
            OpenApiParameter('limit', int, description=f'返回字节上限，默认与最大均为 {CONTENT_LIMIT}'),
            OpenApiParameter('tail', str, description='true 时忽略 offset，读文件末尾 limit 字节'),
        ],
        responses=OpenApiTypes.OBJECT,
    )
    def get(self, request):
        rel = (request.query_params.get('path') or '').strip().strip('/')
        if not rel:
            return fail('缺少查询参数 path（文件路径）')
        path, err = resolve_log_path(rel)
        if err:
            return fail(err)
        if not path.is_file():
            return fail(f'文件不存在或已过保留期：{rel}')

        size = path.stat().st_size
        limit = _positive_int(request.query_params.get('limit'), CONTENT_LIMIT)
        limit = min(limit, CONTENT_LIMIT)
        tail = request.query_params.get('tail') == 'true'
        offset = max(0, size - limit) if tail else _positive_int(
            request.query_params.get('offset'), 0)
        with path.open('rb') as f:
            f.seek(min(offset, size))
            chunk = f.read(limit)
        if b'\x00' in chunk:
            return fail(f'该文件为二进制内容，请下载后查看：{rel}')
        return ok({
            'path': rel,
            'name': path.name,
            'size': size,
            'offset': offset,
            'length': len(chunk),
            'eof': offset + len(chunk) >= size,
            # 日志含 ANSI 转义，原样返回由前端渲染（utils/ansi.js）
            'content': chunk.decode('utf-8', errors='replace'),
        })


class TaskLogFileDownloadView(APIView):
    """任务日志文件下载：原文件流式下载（大日志不进内存、不受内容接口上限限制）。"""

    permission_classes = [module_perm('exec')]

    @extend_schema(
        parameters=[OpenApiParameter('path', str, description='相对 TASK_LOG_DIR 的文件路径，必填')],
        responses=OpenApiTypes.BINARY,
    )
    def get(self, request):
        rel = (request.query_params.get('path') or '').strip().strip('/')
        if not rel:
            return fail('缺少查询参数 path（文件路径）')
        path, err = resolve_log_path(rel)
        if err:
            return fail(err)
        if not path.is_file():
            return fail(f'文件不存在或已过保留期：{rel}')
        # 文件名带上目录层级，避免多次下载的 inventory.ini/vars.yml 互相覆盖
        filename = rel.replace('/', '_')
        return FileResponse(path.open('rb'), as_attachment=True, filename=filename)


def _positive_int(raw, default):
    """查询参数转非负整数，非法值回落默认值。"""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(0, value)
