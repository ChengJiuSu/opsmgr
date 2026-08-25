"""Jenkins 构建方法执行器。

通过 Jenkins REST API 触发指定 job 构建并传参，增量拉取控制台日志同步进
平台执行日志，构建结果映射为模块返回码（SUCCESS=0，其余非 0）。

方法是公共能力，**不绑定具体 job**：job 在任务模块参数（TaskStep.params）
的保留键 job_name 中指定，同一个方法可被不同任务复用触发不同 job。

方法配置（Method.config，均可选）：
    {"default_params": {"BRANCH": "master"},  # 方法级默认构建参数
     "allow_unstable": false}                 # UNSTABLE 是否视为成功

模块参数（TaskStep.params）：
    {"job_name": "build-app",   # 必填（平台保留键，不透传给 Jenkins）
     "BRANCH": "release"}       # 其余键为构建参数，覆盖 default_params 同名项

artifact_version 亦为平台保留键（CD 锁定制品版本用），不透传给 Jenkins。

执行通道两条路：

- **直连**（manager 与 Jenkins 网络可达）：本模块自己发 REST，连接信息读系统
  配置 jenkins.url / jenkins.user / jenkins.api_token；
- **经 Agent**（manager 在公网、Jenkins 在内网）：打成 step_type='jenkins' 的
  执行单元下发给 agent，由 agent 在内网触发并流式回传控制台日志。此路
  **不下发任何 Jenkins 凭据**，凭据只在 agent 本地 config.yaml 维护。

通道解析（agent_mgr.channel.resolve_service_agent，四级取第一个命中）：
    模块参数 params.agent → 方法配置 config.agent
    → 系统配置 jenkins.agent → AgentRoute(任务项目组/环境) → 直连
"""
import re
import time
from urllib.parse import quote

import requests

from .base import BaseExecutor, register

POLL_INTERVAL = 2  # 队列/日志轮询间隔（秒）

# Jenkins ConsoleNote 注解：日志流中形如 \x1b[8mha:<base64>\x1b[0m 的隐藏段
# （Jenkins 网页用它渲染超链接），progressiveText 原样透出，平台展示需剥除。
# ESC 序列可能在传输/存储中丢失，故 ESC 部分按可选匹配。
CONSOLE_NOTE_RE = re.compile(r'(?:\x1b\[8m)?ha:////[A-Za-z0-9+/=]+(?:\x1b\[0m)?')
ANSI_ESCAPE_RE = re.compile(r'\x1b\[[0-9;]*m')

# 平台保留键，不作为 Jenkins 构建参数透传：
#   job_name 指定触发的 job、agent 指定执行通道（本执行器消费）；
#   artifact_version 由 task_exec.runner 消费
RESERVED_PARAM_KEYS = ('job_name', 'artifact_version', 'agent')
# 经 agent 下发时的等待余量已由 dispatch 处理，此处只传模块 timeout
DEFAULT_TIMEOUT = 300


class _JenkinsStepError(Exception):
    """内部流程失败：携带 (rc, output)，在 execute 出口统一转为返回值。"""

    def __init__(self, rc, output):
        super().__init__(output)
        self.rc = rc
        self.output = output


def _resolve_agent(step, config):
    """解析该 Jenkins 模块的执行通道，返回 (agent 或 None, source)。

    候选名按优先级取第一个非空；全空时按任务维度（模块自定义范围优先于任务
    全局）回落到 AgentRoute 规则。
    """
    from apps.agent_mgr.channel import (
        SOURCE_CONFIG, SOURCE_METHOD, SOURCE_STEP, resolve_service_agent,
    )
    from apps.system_cfg.services import get_config

    candidates = [
        (SOURCE_STEP, (step.params or {}).get('agent')),
        (SOURCE_METHOD, config.get('agent')),
        (SOURCE_CONFIG, get_config('jenkins.agent')),
    ]
    scope = step if step.use_custom_scope else step.task
    return resolve_service_agent(
        candidates,
        project_group_id=scope.project_group_id,
        environment_id=scope.environment_id,
    )


def _jenkins_settings():
    from apps.system_cfg.services import get_config

    return (
        get_config('jenkins.url').rstrip('/'),
        get_config('jenkins.user'),
        get_config('jenkins.api_token'),
    )


@register('jenkins')
class JenkinsExecutor(BaseExecutor):

    def validate_config(self, config):
        config = config or {}
        default_params = config.get('default_params')
        if default_params is not None and not isinstance(default_params, dict):
            raise ValueError('config.default_params 必须为 JSON 对象（键值对）')

    def execute(self, task, method, inventory_path=None, servers=None, on_data=None):
        # task 为 TaskStep：params/timeout 为模块级入参；Jenkins 构建不涉及目标主机，
        # 忽略 inventory_path 与 servers。直连路径按轮询整段同步（不走 on_data），
        # 经 agent 路径由 agent 分片回传，on_data 实时可见。
        step = task
        config = method.config or {}
        try:
            self.validate_config(config)
        except ValueError as exc:
            return 1, str(exc)

        step_params = step.params or {}
        # job 由任务模块参数指定（方法为公共能力，不绑定具体 job）；
        # 兼容存量：方法 config 中的 job_name 仍作为兜底默认值
        job_name = str(step_params.get('job_name') or config.get('job_name') or '').strip()
        if not job_name:
            return 1, ('未指定 Jenkins Job：请在任务模块的方法参数中配置，'
                       '如 {"job_name": "build-app", "BRANCH": "master"}。')

        params = dict(config.get('default_params') or {})
        params.update(step_params)
        for key in RESERVED_PARAM_KEYS:
            params.pop(key, None)
        params = {str(k): str(v) for k, v in params.items()}

        from apps.agent_mgr.channel import ChannelError

        try:
            agent, source = _resolve_agent(step, config)
        except ChannelError as exc:
            return 1, str(exc)
        if agent is not None:
            return self._run_via_agent(agent, source, step, job_name, params, config, on_data)
        return self._run_direct(step, job_name, params, config)

    # ── 直连路径 ──────────────────────────────────────────

    def _run_direct(self, step, job_name, params, config):
        base_url, user, token = _jenkins_settings()
        if not (base_url and user and token):
            return 1, ('Jenkins 连接信息不完整，请在「系统配置」维护 '
                       'jenkins.url / jenkins.user / jenkins.api_token；'
                       'Jenkins 在内网时改为配置 jenkins.agent 经 Agent 触发。')

        session = requests.Session()
        session.auth = (user, token)
        # trust_env=False：内网 Jenkins 走环境代理只会拿到 502
        session.trust_env = False
        deadline = time.monotonic() + (step.timeout or DEFAULT_TIMEOUT)

        try:
            return self._trigger_and_wait(session, base_url, job_name, params, config, deadline)
        except _JenkinsStepError as exc:
            return exc.rc, exc.output
        except requests.RequestException as exc:
            return 1, f'Jenkins 请求异常：{exc}'

    # ── Agent 通道路径 ────────────────────────────────────

    def _run_via_agent(self, agent, source, step, job_name, params, config, on_data):
        """打成 jenkins 执行单元下发 agent，阻塞等终态；输出加通道标头。"""
        from apps.agent_mgr.channel import SOURCE_LABELS
        from apps.agent_mgr.dispatch import run_via_agent

        timeout = step.timeout or DEFAULT_TIMEOUT
        payload = {
            'step_type': 'jenkins',
            'job_name': job_name,
            'params': params,
            'allow_unstable': bool(config.get('allow_unstable')),
            'timeout': timeout,
        }
        header = f'───── [经 Agent {agent.name} · {SOURCE_LABELS[source]}] ─────'
        if on_data:
            on_data(header + '\n')
        rc, output = run_via_agent(
            agent, payload, timeout=timeout,
            task_id=step.task_id, step_order=step.order, on_data=on_data,
            capability='jenkins')
        return rc, f'{header}\n{output}'

    # ── 内部流程（失败抛 _JenkinsStepError）────────────────

    def _trigger_and_wait(self, session, base_url, job_name, params, config, deadline):
        queue_url = self._trigger(session, base_url, job_name, params)
        build_url, build_number = self._wait_build_start(session, queue_url, deadline)

        lines = [
            f'[jenkins] job={job_name} build=#{build_number}',
            f'[jenkins] url={build_url}',
            '─' * 30 + ' Jenkins Console Output ' + '─' * 30,
        ]
        rc_timeout = self._stream_console(session, build_url, lines, deadline)
        if rc_timeout is not None:
            return rc_timeout, '\n'.join(lines)

        result = self._final_result(session, build_url, deadline)
        allow_unstable = bool(config.get('allow_unstable'))
        rc = 0 if result == 'SUCCESS' or (result == 'UNSTABLE' and allow_unstable) else 1
        lines.append('─' * 84)
        lines.append(f'[jenkins] 构建结果：{result or "未知（等待结果超时）"}')
        return rc, '\n'.join(lines)

    def _trigger(self, session, base_url, job_name, params):
        """触发构建，返回队列 URL。"""
        action = 'buildWithParameters' if params else 'build'
        endpoint = f'{base_url}/job/{quote(job_name)}/{action}'
        resp = session.post(endpoint, params=params, timeout=30, allow_redirects=False)
        if resp.status_code == 403:
            # 旧版 Jenkins / 密码模式需要 CSRF crumb，取一次后重试
            crumb = session.get(f'{base_url}/crumbIssuer/api/json', timeout=30)
            if crumb.ok:
                data = crumb.json()
                session.headers[data['crumbRequestField']] = data['crumb']
                resp = session.post(endpoint, params=params, timeout=30, allow_redirects=False)
        if resp.status_code != 201:
            raise _JenkinsStepError(1, (f'触发 Jenkins 构建失败：HTTP {resp.status_code} '
                                        f'{(resp.text or "")[:500]}\n[jenkins] endpoint={endpoint}'))
        queue_url = resp.headers.get('Location', '')
        if not queue_url:
            raise _JenkinsStepError(1, '触发成功但 Jenkins 未返回队列地址（Location 头缺失），无法跟踪构建。')
        return queue_url.rstrip('/') + '/'

    def _wait_build_start(self, session, queue_url, deadline):
        """轮询队列项直至分配 build number，返回 (build_url, number)。"""
        while True:
            resp = session.get(f'{queue_url}api/json', timeout=30)
            if resp.ok:
                item = resp.json()
                if item.get('cancelled'):
                    raise _JenkinsStepError(1, '构建在 Jenkins 队列中被取消。')
                executable = item.get('executable') or {}
                if executable.get('url'):
                    return executable['url'].rstrip('/') + '/', executable.get('number')
            if time.monotonic() >= deadline:
                raise _JenkinsStepError(1, f'等待 Jenkins 构建出队超时（队列：{queue_url}）。')
            time.sleep(POLL_INTERVAL)

    def _stream_console(self, session, build_url, lines, deadline):
        """增量拉取控制台日志追加进 lines；超时返回 rc=124，正常结束返回 None。"""
        start = 0
        while True:
            resp = session.get(f'{build_url}logText/progressiveText',
                               params={'start': start}, timeout=30)
            if resp.ok and resp.text:
                text = CONSOLE_NOTE_RE.sub('', resp.text)
                text = ANSI_ESCAPE_RE.sub('', text)
                lines.append(text.rstrip('\n'))
            start = int(resp.headers.get('X-Text-Size', start) or start)
            if resp.headers.get('X-More-Data') != 'true':
                return None
            if time.monotonic() >= deadline:
                lines.append(f'✖ 等待 Jenkins 构建完成超时（构建可能仍在运行：{build_url}）。')
                return 124
            time.sleep(POLL_INTERVAL)

    def _final_result(self, session, build_url, deadline):
        """取构建结果。日志结束时 result 可能尚未落库，短轮询兜底。"""
        while True:
            resp = session.get(f'{build_url}api/json', timeout=30)
            if resp.ok:
                result = resp.json().get('result')
                if result:
                    return result
            if time.monotonic() >= deadline:
                return None
            time.sleep(POLL_INTERVAL)
