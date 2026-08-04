"""Ansible 执行公共工具：环境变量与子进程调用。

被命令执行（task_exec 动态 inventory ad-hoc）与方法执行
（method_mgr 各 provider）共用，放在 common 下避免 app 间循环依赖。

对外两个执行入口：
  - run_stream(..., on_data=cb)：边跑边把输出分片回调出去（任务日志实时刷新），
    posix 下挂伪终端（pty）执行，使 ansible 认为输出是终端 → 行级 flush + 彩色；
  - run_subprocess(...)：run_stream 的无回调封装，保持既有调用方语义不变。
"""
import codecs
import logging
import os
import signal
import subprocess
import sys
import threading

logger = logging.getLogger('apps.task_exec')

# 读取块大小；进程组终止后等待退出的宽限期（秒）
READ_SIZE = 8192
KILL_GRACE = 3


def build_env():
    """构造关闭交互/主机指纹校验的 Ansible 运行环境。"""
    from django.conf import settings

    env = os.environ.copy()
    # ansible 通常与本服务装在同一 venv，PATH 头部插入解释器目录，
    # 保证未 activate venv / systemd 默认 PATH 下也能找到可执行文件
    venv_bin = os.path.dirname(sys.executable)
    env['PATH'] = venv_bin + os.pathsep + env.get('PATH', '')
    env['ANSIBLE_HOST_KEY_CHECKING'] = 'False'
    env['ANSIBLE_RETRY_FILES_ENABLED'] = 'False'
    # 彩色输出：由 settings.TASK_LOG_COLOR 控制（前端渲染 ANSI；关掉则纯文本）。
    # 注意环境变量优先于 ansible.cfg 的 force_color，此处必须显式给值
    env['ANSIBLE_FORCE_COLOR'] = 'True' if getattr(settings, 'TASK_LOG_COLOR', True) else 'False'
    env['ANSIBLE_NOCOWS'] = '1'
    env['PYTHONUNBUFFERED'] = '1'
    return env


def _kill_tree(proc):
    """终止子进程及其整个进程组（ansible 会派生 ssh 子进程）。"""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (OSError, AttributeError):  # 进程已退出 / 非 posix
        proc.terminate()
    try:
        proc.wait(timeout=KILL_GRACE)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, AttributeError):
            proc.kill()


def run_stream(cmd, timeout, cwd=None, env=None, on_data=None):
    """执行命令并流式回调输出分片，返回 (return_code, output)。

    on_data(text) 为可选回调（异常会被吞掉并记日志，不影响执行本体）；
    超时按进程组终止，返回 rc=124；可执行文件缺失返回 rc=127。
    """
    logger.info('执行命令: %s (cwd=%s)', ' '.join(cmd), cwd or '.')
    use_pty = bool(on_data) and hasattr(os, 'openpty')
    master = slave = None
    try:
        if use_pty:
            master, slave = os.openpty()
        proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL,
            stdout=slave if use_pty else subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=cwd, env=env or build_env(), start_new_session=True,
        )
    except FileNotFoundError as exc:
        for fd in (master, slave):
            if fd is not None:
                os.close(fd)
        return 127, f'未找到可执行程序：{exc}. 请确认已安装 Ansible。'
    if slave is not None:
        os.close(slave)  # 父进程持有会导致读端永不 EOF

    timed_out = threading.Event()

    def _on_timeout():
        timed_out.set()
        _kill_tree(proc)

    timer = threading.Timer(timeout, _on_timeout)
    timer.start()

    decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
    parts, pending_cr = [], ''
    try:
        while True:
            try:
                # read1：拿到即返回，不等凑满缓冲（流式的关键）
                raw = os.read(master, READ_SIZE) if use_pty else proc.stdout.read1(READ_SIZE)
            except OSError:
                break  # pty 对端关闭（EIO）= 子进程结束
            if not raw:
                break
            text = pending_cr + decoder.decode(raw)
            pending_cr = ''
            if text.endswith('\r'):  # \r\n 可能跨分片，留到下一片再合
                text, pending_cr = text[:-1], '\r'
            text = text.replace('\r\n', '\n')  # pty 回车换行归一，避免前端多空行
            if not text:
                continue
            parts.append(text)
            if on_data:
                try:
                    on_data(text)
                except Exception:  # noqa: BLE001 - 回调（落库/落盘）失败不应中断执行
                    logger.exception('输出流回调失败')
    finally:
        timer.cancel()
        if use_pty:
            os.close(master)
        elif proc.stdout:
            proc.stdout.close()
    if pending_cr:
        parts.append(pending_cr)

    try:
        rc = proc.wait(timeout=KILL_GRACE)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        rc = proc.returncode if proc.returncode is not None else -9

    output = ''.join(parts)
    if timed_out.is_set():
        note = f'\n✖ 任务执行超时（>{timeout}s），已终止。'
        if on_data:
            try:
                on_data(note)
            except Exception:  # noqa: BLE001
                logger.exception('输出流回调失败')
        return 124, output + note
    return rc, output


def run_subprocess(cmd, timeout, cwd=None, env=None):
    """执行命令，返回 (return_code, output)。捕获超时与可执行文件缺失。"""
    return run_stream(cmd, timeout, cwd=cwd, env=env)


def resolve_within(project_dir, rel_or_abs):
    """解析路径并防止越出项目目录（路径穿越），越界抛 ValueError。"""
    base = os.path.realpath(project_dir)
    target = rel_or_abs if os.path.isabs(rel_or_abs) else os.path.join(base, rel_or_abs)
    target = os.path.realpath(target)
    if not (target == base or target.startswith(base + os.sep)):
        raise ValueError(f'路径越出 Ansible 项目目录：{rel_or_abs}')
    return target
