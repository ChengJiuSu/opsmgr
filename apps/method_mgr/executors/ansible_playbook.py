"""Ansible 剧本方法执行器。

在集中管理的 Ansible 项目目录内执行既有剧本，通过 --tags 触发对应能力；
执行主机来自任务所选服务器（task_exec.runner 生成动态 inventory 传入），
平台按需传入 tags/limit/extra-vars。
"""
import os

from django.conf import settings

from common.ansible import resolve_within, run_subprocess

from .base import BaseExecutor, register


@register('ansible_playbook')
class AnsiblePlaybookExecutor(BaseExecutor):

    def validate_config(self, config):
        if not (config or {}).get('playbook'):
            raise ValueError('ansible_playbook 方法必须在 config.playbook 指定剧本文件')

    def execute(self, task, method, inventory_path=None, servers=None):
        project_dir = getattr(settings, 'ANSIBLE_PROJECT_DIR', '')
        if not project_dir or not os.path.isdir(project_dir):
            return 1, f'Ansible 项目目录不存在：{project_dir!r}（请配置 ANSIBLE_PROJECT_DIR）'
        if not inventory_path:
            return 1, '缺少动态 inventory（方法任务必须通过任务执行入口调度）。'

        config = method.config or {}
        try:
            self.validate_config(config)
            playbook_path = resolve_within(project_dir, config['playbook'])
        except ValueError as exc:
            return 1, str(exc)
        if not os.path.isfile(playbook_path):
            return 1, f'剧本文件不存在：{config["playbook"]}'

        # 模块级覆盖方法默认值（task 为 TaskStep，tags/extra_vars/timeout 均模块级）
        from apps.task_exec.runner import extra_vars_args

        tags = (task.tags or config.get('tags') or '').strip()
        limit = (config.get('limit') or '').strip()

        cmd = ['ansible-playbook', playbook_path, '-i', inventory_path]
        if getattr(task, 'exec_mode', '') == 'serial':
            cmd += ['--forks', '1']  # 串行：逐台执行
        if tags:
            cmd += ['--tags', tags]
        if limit:
            cmd += ['--limit', limit]
        # 变量管理启用变量 + 模块额外变量（后者覆盖同名项）
        cmd += extra_vars_args(task)

        return run_subprocess(cmd, timeout=task.timeout or 300, cwd=project_dir)
