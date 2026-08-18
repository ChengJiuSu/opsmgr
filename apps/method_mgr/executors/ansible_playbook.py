"""Ansible 剧本方法执行器。

在集中管理的 Ansible 项目目录内执行既有剧本，通过 --tags 触发对应能力；
执行主机来自任务所选服务器（task_exec.runner 生成动态 inventory 传入），
平台按需传入 tags/limit/extra-vars。
"""
import os

from django.conf import settings

from common.ansible import resolve_within, run_stream

from .base import BaseExecutor, register


@register('ansible_playbook')
class AnsiblePlaybookExecutor(BaseExecutor):

    def validate_config(self, config):
        if not (config or {}).get('playbook'):
            raise ValueError('ansible_playbook 方法必须在 config.playbook 指定剧本文件')

    def execute(self, task, method, inventory_path=None, servers=None, on_data=None):
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
        from apps.task_exec.runner import _forks_args, extra_vars_args

        tags = (task.tags or config.get('tags') or '').strip()
        limit = (config.get('limit') or '').strip()

        # 主机并发与命令/playbook 模块同口径：串行逐台，并发按任务并发数分批
        cmd = ['ansible-playbook', playbook_path, '-i', inventory_path] + _forks_args(task)
        if tags:
            cmd += ['--tags', tags]
        if limit:
            cmd += ['--limit', limit]
        # 变量管理启用变量 + 模块额外变量（后者覆盖同名项）；变量文件与 inventory
        # 同处 runner 建的临时工作目录，执行结束由 runner 统一清理
        cmd += extra_vars_args(task, os.path.dirname(inventory_path))

        return run_stream(cmd, timeout=task.timeout or 300, cwd=project_dir, on_data=on_data)
