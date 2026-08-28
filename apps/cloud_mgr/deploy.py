"""证书分发执行层。

- server（直连）：复用 task_exec 的动态 inventory 与 ansible 子进程口径，生成
  临时剧本推送 crt/key/fullchain（私钥 0600）到部署目录，随后执行重载命令；
- server（agent 通道）：同一份文件清单打成 `step_type='cert_deploy'` payload
  下发，剧本由 agent 本地按同口径生成（agent 侧不接受任意剧本内容）。证书
  内容随 payload 过通道，但**不落 AgentJob.payload**（dispatch secret_fields
  脱敏），agent 侧同样只写 0600 临时目录、执行完即删；
- lb/ga/cdn：各自 deployer 为桩，签名已定，对应云资源模块接入后实现。

同步阻塞（ansible 子进程），调用方负责放后台线程/celery，见 tasks.py。
"""
import json
import logging
import os
import re
import shutil
import tempfile

from common.ansible import run_subprocess

logger = logging.getLogger(__name__)

# 部署超时（含 reload），单位秒
DEPLOY_TIMEOUT = 300


class CertDeployError(Exception):
    """分发失败，message 面向用户展示。"""


def safe_cert_name(certificate):
    """证书文件名前缀：与下载包内文件名同口径。"""
    return re.sub(r'[^\w.*-]', '_', certificate.name) or f'cert-{certificate.pk}'


def cert_files(certificate):
    """待分发文件清单 [(本地临时名, 目标文件名, mode, 内容)]。

    直连与 agent 两条路共用，保证两侧文件名/权限口径一致。
    """
    name = safe_cert_name(certificate)
    files = [('cert.pem', f'{name}.crt', '0644', certificate.cert_pem)]
    if certificate.chain_pem:
        files.append(('fullchain.pem', f'{name}.fullchain.pem', '0644', certificate.chain_pem))
    files.append(('key.pem', f'{name}.key', '0600', certificate.private_key_pem))
    return files


def _build_playbook(certificate, deployment, workdir):
    """生成分发剧本（JSON 即合法 YAML，规避引号转义），返回剧本路径。"""
    files = cert_files(certificate)

    tasks = [{
        'name': '创建证书目录',
        'ansible.builtin.file': {
            'path': deployment.deploy_path, 'state': 'directory', 'mode': '0750',
        },
    }]
    for local_name, dest_name, mode, content in files:
        local_path = os.path.join(workdir, local_name)
        with open(local_path, 'w', encoding='utf-8') as f:
            f.write(content)
        os.chmod(local_path, 0o600)
        tasks.append({
            'name': f'分发 {dest_name}',
            'ansible.builtin.copy': {
                'src': local_path,
                'dest': os.path.join(deployment.deploy_path, dest_name),
                'mode': mode,
                'backup': True,  # 覆盖前留旧副本，便于回滚
            },
        })
    if (deployment.reload_cmd or '').strip():
        tasks.append({
            'name': '重载服务',
            'ansible.builtin.shell': deployment.reload_cmd.strip(),
        })

    playbook = [{'hosts': 'all', 'gather_facts': False, 'tasks': tasks}]
    playbook_path = os.path.join(workdir, 'deploy_cert.yml')
    with open(playbook_path, 'w', encoding='utf-8') as f:
        json.dump(playbook, f, ensure_ascii=False)
    return playbook_path


def _deploy_via_agent(certificate, deployment, server, agent):
    """经 agent 通道分发：打 cert_deploy payload 下发，阻塞等回传。

    inventory 用无密钥版本（SSH 凭据驻留 agent 主机，同 task_exec 口径）；
    证书内容放 files，仅下发不落库（secret_fields）。
    """
    from apps.agent_mgr.dispatch import run_via_agent
    from apps.task_exec.runner import _inventory_content

    payload = {
        'step_type': 'cert_deploy',
        'inventory': _inventory_content(
            [server], deployment.credential, None, include_secrets=False),
        'deploy_path': deployment.deploy_path.strip(),
        'reload_cmd': (deployment.reload_cmd or '').strip(),
        'files': [
            {'name': dest_name, 'mode': mode, 'content': content}
            for _local, dest_name, mode, content in cert_files(certificate)
        ],
        'forks': 1,
        'timeout': DEPLOY_TIMEOUT,
    }
    rc, output = run_via_agent(
        agent, payload, timeout=DEPLOY_TIMEOUT,
        capability='cert_deploy', secret_fields=('files',))
    if rc != 0:
        raise CertDeployError(
            f'经 Agent {agent.name} 分发失败（rc={rc}）：\n{(output or "").strip()[-2000:]}')
    return output


def deploy_to_server(certificate, deployment):
    """推送证书到目标服务器并执行重载。返回执行输出，失败抛 CertDeployError。

    按主机执行通道分两路：直连本地起 ansible，agent 通道经 dispatch 下发。
    """
    from apps.agent_mgr.channel import resolve_agent
    from apps.task_exec.runner import _write_inventory

    server = deployment.server
    if server is None:
        raise CertDeployError('分发目标服务器已不存在（可能已被删除），请重新选择。')
    if not (deployment.deploy_path or '').strip():
        raise CertDeployError('未配置部署目录。')

    channel_agent = resolve_agent(server)
    if channel_agent:
        return _deploy_via_agent(certificate, deployment, server, channel_agent)

    workdir = tempfile.mkdtemp(prefix='ops_cert_deploy_')
    try:
        inventory_path = _write_inventory([server], deployment.credential, workdir)
        playbook_path = _build_playbook(certificate, deployment, workdir)
        rc, output = run_subprocess(
            ['ansible-playbook', playbook_path, '-i', inventory_path, '--forks', '1'],
            timeout=DEPLOY_TIMEOUT,
        )
    finally:
        # 目录内含私钥明文副本，务必清理
        shutil.rmtree(workdir, ignore_errors=True)

    if rc != 0:
        raise CertDeployError(f'ansible 执行失败（rc={rc}）：\n{output.strip()[-2000:]}')
    return output


def _deploy_lb(certificate, deployment):
    raise CertDeployError('负载均衡（LB）证书分发暂未接入，接口已预留，等 LB 资源模块上线后支持。')


def _deploy_ga(certificate, deployment):
    raise CertDeployError('全球加速（GA）证书分发暂未接入，接口已预留，等 GA 资源模块上线后支持。')


def _deploy_cdn(certificate, deployment):
    raise CertDeployError('CDN 证书分发暂未接入，接口已预留，等 CDN 资源模块上线后支持。')


# 目标类型 → 分发实现（新增目标类型在此登记）
DEPLOYERS = {
    'server': deploy_to_server,
    'lb': _deploy_lb,
    'ga': _deploy_ga,
    'cdn': _deploy_cdn,
}


def supported_target_types():
    """已实现分发的目标类型（前端据此禁用未接入项）。"""
    return ['server']


def run_deployment(certificate, deployment):
    """按目标类型分派分发；返回执行输出，失败抛 CertDeployError。"""
    deployer = DEPLOYERS.get(deployment.target_type)
    if deployer is None:
        raise CertDeployError(f'未知分发目标类型：{deployment.target_type}')
    return deployer(certificate, deployment)
