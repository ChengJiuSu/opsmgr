"""Ansible 剧本方法执行器。

在集中管理的 Ansible 项目目录内执行既有剧本，通过 --tags 触发对应能力；
主机与 inventory 由 Ansible 项目统一维护，平台按需传入 tags/limit/extra-vars。
"""
import os

from django.conf import settings

from common.ansible import run_subprocess

from .base import BaseExecutor, register


@register('ansible_playbook')
class AnsiblePlaybookExecutor(BaseExecutor):

    def validate_config(self, config):
        if not (config or {}).get('playbook'):
            raise ValueError('ansible_playbook 方法必须在 config.playbook 指定剧本文件')

    def _resolve_within(self, project_dir, rel_or_abs):
        """解析路径并防止越出项目目录（路径穿越）。"""
        base = os.path.realpath(project_dir)
        target = rel_or_abs if os.path.isabs(rel_or_abs) else os.path.join(base, rel_or_abs)
        target = os.path.realpath(target)
        if not (target == base or target.startswith(base + os.sep)):
            raise ValueError(f'路径越出 Ansible 项目目录：{rel_or_abs}')
        return target

    def execute(self, task, method):
        project_dir = getattr(settings, 'ANSIBLE_PROJECT_DIR', '')
        if not project_dir or not os.path.isdir(project_dir):
            return 1, f'Ansible 项目目录不存在：{project_dir!r}（请配置 ANSIBLE_PROJECT_DIR）'

        config = method.config or {}
        try:
            self.validate_config(config)
            playbook_path = self._resolve_within(project_dir, config['playbook'])
        except ValueError as exc:
            return 1, str(exc)
        if not os.path.isfile(playbook_path):
            return 1, f'剧本文件不存在：{config["playbook"]}'

        # 任务级覆盖方法默认值
        tags = (task.tags or config.get('tags') or '').strip()
        limit = (task.limit or config.get('limit') or '').strip()
        inventory = (task.inventory or config.get('inventory') or getattr(settings, 'ANSIBLE_INVENTORY', '')).strip()
        extra_vars = (task.extra_vars or '').strip()

        cmd = ['ansible-playbook', playbook_path]
        if inventory:
            try:
                inv_path = self._resolve_within(project_dir, inventory)
            except ValueError as exc:
                return 1, str(exc)
            cmd += ['-i', inv_path]
        if tags:
            cmd += ['--tags', tags]
        if limit:
            cmd += ['--limit', limit]
        if extra_vars:
            cmd += ['--extra-vars', extra_vars]

        return run_subprocess(cmd, timeout=task.timeout or 300, cwd=project_dir)
