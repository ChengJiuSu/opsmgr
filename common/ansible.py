"""Ansible 执行公共工具：环境变量与子进程调用。

被命令执行（task_exec 动态 inventory ad-hoc）与方法执行
（method_mgr 各 provider）共用，放在 common 下避免 app 间循环依赖。
"""
import logging
import os
import subprocess
import sys

logger = logging.getLogger('apps.task_exec')


def build_env():
    """构造关闭交互/主机指纹校验的 Ansible 运行环境。"""
    env = os.environ.copy()
    # ansible 通常与本服务装在同一 venv，PATH 头部插入解释器目录，
    # 保证未 activate venv / systemd 默认 PATH 下也能找到可执行文件
    venv_bin = os.path.dirname(sys.executable)
    env['PATH'] = venv_bin + os.pathsep + env.get('PATH', '')
    env['ANSIBLE_HOST_KEY_CHECKING'] = 'False'
    env['ANSIBLE_RETRY_FILES_ENABLED'] = 'False'
    env['ANSIBLE_FORCE_COLOR'] = 'False'
    env['ANSIBLE_NOCOWS'] = '1'
    return env


def run_subprocess(cmd, timeout, cwd=None, env=None):
    """执行命令，返回 (return_code, output)。捕获超时与可执行文件缺失。"""
    logger.info('执行命令: %s (cwd=%s)', ' '.join(cmd), cwd or '.')
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            env=env or build_env(), timeout=timeout, cwd=cwd,
        )
        return proc.returncode, (proc.stdout or '') + (proc.stderr or '')
    except subprocess.TimeoutExpired:
        return 124, f'任务执行超时（>{timeout}s），已终止。'
    except FileNotFoundError as exc:
        return 127, f'未找到可执行程序：{exc}. 请确认已安装 Ansible。'


def resolve_within(project_dir, rel_or_abs):
    """解析路径并防止越出项目目录（路径穿越），越界抛 ValueError。"""
    base = os.path.realpath(project_dir)
    target = rel_or_abs if os.path.isabs(rel_or_abs) else os.path.join(base, rel_or_abs)
    target = os.path.realpath(target)
    if not (target == base or target.startswith(base + os.sep)):
        raise ValueError(f'路径越出 Ansible 项目目录：{rel_or_abs}')
    return target
