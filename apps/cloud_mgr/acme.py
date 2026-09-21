"""acme.sh 证书签发/续签封装（DNS API 验证）。

- DNS API 模式（--dns dns_ali / dns_tencent）：acme.sh 自动增删
  `_acme-challenge` TXT 记录，验证完成即删，解析列表保持干净；
- AK/SK 从 CloudAccount 注入子进程环境变量，不落盘不入日志；
- 签发成功用 --install-cert 导出到临时目录读取 PEM 入库，过期时间用
  openssl 解析（失败回退签发时间 + 90 天，Let's Encrypt 口径）。
- 写完 TXT 记录用 --dnssleep 硬等传播，失败若是 DNS 传播类错误再自动重试一次
  （见 ACME_DNS_SLEEP / ACME_DNS_RETRY_WAIT）；

同步阻塞（数分钟），调用方负责放后台线程/celery，见 tasks.py。
"""
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone as dt_timezone

from django.conf import settings

logger = logging.getLogger(__name__)


class AcmeError(Exception):
    """acme.sh 调用失败，message 面向用户展示（不含密钥）。"""


# CA 侧查不到 TXT 记录的报错特征：DNS 传播没跟上，等负缓存过期重试一次即可
DNS_PROPAGATION_MARKERS = (
    'DNS problem',
    'NXDOMAIN',
    'no TXT record',
    'incorrect TXT record',
)


def _is_dns_propagation_error(message):
    return any(m.lower() in message.lower() for m in DNS_PROPAGATION_MARKERS)


# CloudProvider.code → (acme.sh dns hook, 凭据环境变量名)
ACME_DNS_HOOKS = {
    'aliyun': ('dns_ali', {'Ali_Key': 'access_key', 'Ali_Secret': 'secret_key'}),
    'tencent': ('dns_tencent', {'Tencent_SecretId': 'access_key', 'Tencent_SecretKey': 'secret_key'}),
}


def supported_provider_codes():
    return list(ACME_DNS_HOOKS)


def _acme_env(cloud_account):
    """构造 acme.sh 子进程环境：注入 DNS API 凭据。"""
    code = cloud_account.provider.code
    if code not in ACME_DNS_HOOKS:
        raise AcmeError(
            f'云厂商 {cloud_account.provider.name}({code}) 暂不支持证书 DNS 验证，'
            f'当前支持：{"、".join(ACME_DNS_HOOKS)}'
        )
    _, env_map = ACME_DNS_HOOKS[code]
    env = os.environ.copy()
    # 清掉代理环境变量：与 DNS 适配层同口径，防 WSL 代理劫持 ACME/云 API 出口
    for key in ('http_proxy', 'https_proxy', 'all_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY'):
        env.pop(key, None)
    for env_key, attr in env_map.items():
        env[env_key] = getattr(cloud_account, attr)
    return env


def _run_acme(args, env, timeout=None):
    """执行 acme.sh，返回 stdout+stderr；非零退出抛 AcmeError（截尾部输出）。"""
    acme_bin = settings.ACME_BIN
    if not os.path.isfile(acme_bin):
        raise AcmeError(
            f'未找到 acme.sh（{acme_bin}）。安装：curl https://get.acme.sh | sh，'
            f'或配置 ACME_BIN 指向已有安装。'
        )
    os.makedirs(settings.ACME_HOME, exist_ok=True)
    cmd = ['sh', acme_bin, '--home', settings.ACME_HOME] + args
    # 密钥仅在环境变量中，args 可安全入日志
    logger.info('执行 acme.sh: %s', ' '.join(args))
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, env=env,
            timeout=timeout or settings.ACME_TIMEOUT,
        )
    except subprocess.TimeoutExpired as e:
        raise AcmeError(f'acme.sh 执行超时（>{timeout or settings.ACME_TIMEOUT}s）') from e
    output = (proc.stdout or '') + (proc.stderr or '')
    if proc.returncode != 0:
        # 只保留尾部关键输出，避免超长日志入库
        tail = output.strip()[-2000:]
        raise AcmeError(f'acme.sh 退出码 {proc.returncode}：\n{tail}')
    return output


def _ensure_account(env):
    """注册 ACME 账号（幂等）：配置了邮箱时执行，未配置跳过（acme.sh 会用默认账号）。"""
    email = settings.ACME_EMAIL
    if not email:
        return
    try:
        _run_acme(['--register-account', '-m', email, '--server', settings.ACME_SERVER], env, timeout=120)
    except AcmeError as e:
        # 已注册等情况不阻断签发流程
        logger.warning('ACME 账号注册跳过：%s', e)


def _parse_expiry(cert_pem, fallback_start):
    """openssl 解析证书过期时间；解析失败回退签发时间 + 90 天。"""
    try:
        proc = subprocess.run(
            ['openssl', 'x509', '-noout', '-enddate'],
            input=cert_pem, capture_output=True, text=True, timeout=15,
        )
        m = re.search(r'notAfter=(.+)', proc.stdout)
        if m:
            # 格式如 "Oct 28 12:00:00 2026 GMT"
            return datetime.strptime(m.group(1).strip(), '%b %d %H:%M:%S %Y %Z').replace(
                tzinfo=dt_timezone.utc,
            )
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        logger.warning('openssl 解析证书过期时间失败：%s', e)
    return fallback_start + timedelta(days=90)


def issue_certificate(certificate, force=False):
    """签发（或续签，force=True 强制重签）证书，返回 (cert_pem, key_pem, chain_pem, expires_at)。

    调用方负责证书状态流转与落库。
    """
    domains = certificate.domains
    if not domains:
        raise AcmeError('证书未配置域名列表')
    env = _acme_env(certificate.cloud_account)
    dns_hook = ACME_DNS_HOOKS[certificate.cloud_account.provider.code][0]

    _ensure_account(env)

    issue_args = ['--issue', '--dns', dns_hook, '--server', settings.ACME_SERVER]
    for d in domains:
        issue_args += ['-d', d]
    # 写完 TXT 记录先干等传播：acme.sh 自带的公共 DNS 探测查的是 DoH 解析器，
    # 查得到不代表 CA 的解析器查得到——CA 那边一次 NXDOMAIN 会被负缓存住
    issue_args += ['--dnssleep', str(settings.ACME_DNS_SLEEP)]
    if force:
        issue_args.append('--force')
    try:
        _run_acme(issue_args, env)
    except AcmeError as e:
        wait = settings.ACME_DNS_RETRY_WAIT
        if not wait or not _is_dns_propagation_error(str(e)):
            raise
        # 传播类失败重试一次：等过 CA 侧负缓存 TTL（权威 SOA minimum）再签。
        # 重试必须带 --force，否则 acme.sh 可能因订单未过期直接跳过
        logger.warning('证书签发疑似 DNS 传播失败，%ss 后重试一次：%s', wait, e)
        time.sleep(wait)
        retry_args = issue_args if force else issue_args + ['--force']
        _run_acme(retry_args, env)

    # --install-cert 导出到临时目录读取（不猜 acme home 内部目录布局）
    workdir = tempfile.mkdtemp(prefix='ops_cert_')
    try:
        cert_file = os.path.join(workdir, 'cert.pem')
        key_file = os.path.join(workdir, 'key.pem')
        chain_file = os.path.join(workdir, 'chain.pem')
        _run_acme([
            '--install-cert', '-d', domains[0], '--ecc',
            '--cert-file', cert_file,
            '--key-file', key_file,
            '--fullchain-file', chain_file,
        ], env, timeout=120)
        with open(cert_file) as f:
            cert_pem = f.read()
        with open(key_file) as f:
            key_pem = f.read()
        with open(chain_file) as f:
            chain_pem = f.read()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    now = datetime.now(dt_timezone.utc)
    return cert_pem, key_pem, chain_pem, _parse_expiry(cert_pem, now)
