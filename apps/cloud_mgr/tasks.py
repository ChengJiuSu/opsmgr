"""证书签发/续签/分发调度。

- run_issue：同步执行完整签发流程并落库（状态 issuing → issued/failed），
  成功后自动分发 auto_deploy 的分发目标（续签即自动推送）；
- run_deploy：同步执行一个分发目标并落库（deploying → success/failed）；
- dispatch_issue / dispatch_deploy：按 TASK_EXEC_BACKEND 调度
  （thread 默认 / celery / sync），与 task_exec 同口径；
- renew_expiring_certificates：celery beat 每日任务，auto_renew 且
  距过期不足 CERT_RENEW_BEFORE_DAYS 天的已签发证书强制重签。
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.db import connection
from django.utils import timezone

from .acme import AcmeError, issue_certificate
from .deploy import CertDeployError, run_deployment

logger = logging.getLogger(__name__)


def run_issue(certificate_id, force=False):
    """执行签发并落库。同步阻塞（数分钟），勿在请求线程直接调用。"""
    from .models import Certificate

    cert = Certificate.objects.get(pk=certificate_id)
    try:
        cert_pem, key_pem, chain_pem, expires_at = issue_certificate(cert, force=force)
    except AcmeError as e:
        cert.status = 'failed'
        cert.last_error = str(e)
        cert.save(update_fields=['status', 'last_error', 'updated_at'])
        logger.error('证书 %s 签发失败：%s', cert.name, e)
        return
    cert.cert_pem = cert_pem
    cert.private_key_pem = key_pem
    cert.chain_pem = chain_pem
    cert.issued_at = timezone.now()
    cert.expires_at = expires_at
    cert.status = 'issued'
    cert.last_error = ''
    cert.save(update_fields=[
        'cert_pem', 'private_key_pem', 'chain_pem',
        'issued_at', 'expires_at', 'status', 'last_error', 'updated_at',
    ])
    logger.info('证书 %s 签发成功，过期时间 %s', cert.name, expires_at)
    # 续签后自动推送到已登记目标（同线程顺序执行，避免并发抢同一主机）
    for deployment in cert.deployments.filter(auto_deploy=True):
        run_deploy(deployment.id)


def _run_in_thread(certificate_id, force):
    import threading

    def _worker():
        try:
            run_issue(certificate_id, force=force)
        finally:
            connection.close()  # 线程退出前释放数据库连接

    threading.Thread(target=_worker, daemon=True).start()


def dispatch_issue(certificate, force=False):
    """置 issuing 状态并按配置后端调度签发。"""
    certificate.status = 'issuing'
    certificate.last_error = ''
    certificate.save(update_fields=['status', 'last_error', 'updated_at'])
    backend = getattr(settings, 'TASK_EXEC_BACKEND', 'thread')
    if backend == 'sync':
        run_issue(certificate.id, force=force)
    elif backend == 'celery':
        run_issue_certificate.delay(certificate.id, force=force)
    else:
        _run_in_thread(certificate.id, force)


def run_deploy(deployment_id):
    """执行一个分发目标并落库。同步阻塞（ansible 子进程）。"""
    from .models import CertDeployment

    deployment = CertDeployment.objects.select_related('certificate', 'server', 'credential').get(
        pk=deployment_id,
    )
    cert = deployment.certificate
    if cert.status != 'issued' or not cert.cert_pem:
        deployment.status = 'failed'
        deployment.last_error = '证书尚未签发，无法分发。'
        deployment.save(update_fields=['status', 'last_error', 'updated_at'])
        return
    deployment.status = 'deploying'
    deployment.last_error = ''
    deployment.save(update_fields=['status', 'last_error', 'updated_at'])
    try:
        output = run_deployment(cert, deployment)
    except CertDeployError as e:
        deployment.status = 'failed'
        deployment.last_error = str(e)
        deployment.save(update_fields=['status', 'last_error', 'updated_at'])
        logger.error('证书 %s 分发到 %s 失败：%s', cert.name, deployment, e)
        return
    deployment.status = 'success'
    deployment.last_error = ''
    deployment.last_output = output[-4000:]
    deployment.last_deployed_at = timezone.now()
    deployment.save(update_fields=[
        'status', 'last_error', 'last_output', 'last_deployed_at', 'updated_at',
    ])
    logger.info('证书 %s 分发到 %s 成功', cert.name, deployment)


def _deploy_in_thread(deployment_id):
    import threading

    def _worker():
        try:
            run_deploy(deployment_id)
        finally:
            connection.close()

    threading.Thread(target=_worker, daemon=True).start()


def dispatch_deploy(deployment):
    """置 deploying 状态并按配置后端调度分发。"""
    deployment.status = 'deploying'
    deployment.last_error = ''
    deployment.save(update_fields=['status', 'last_error', 'updated_at'])
    backend = getattr(settings, 'TASK_EXEC_BACKEND', 'thread')
    if backend == 'sync':
        run_deploy(deployment.id)
    elif backend == 'celery':
        run_deploy_certificate.delay(deployment.id)
    else:
        _deploy_in_thread(deployment.id)


def renew_expiring(now=None):
    """续签扫描：返回本次触发续签的证书 id 列表（beat 任务与测试共用）。"""
    from .models import Certificate

    now = now or timezone.now()
    deadline = now + timedelta(days=settings.CERT_RENEW_BEFORE_DAYS)
    expiring = Certificate.objects.filter(
        auto_renew=True, status='issued', expires_at__lt=deadline,
    )
    triggered = []
    for cert in expiring:
        logger.info('证书 %s 距过期不足 %s 天，触发自动续签', cert.name, settings.CERT_RENEW_BEFORE_DAYS)
        dispatch_issue(cert, force=True)
        triggered.append(cert.id)
    return triggered


# ── Celery 任务（仅在已安装 celery 时可用）─────────────────
try:
    from celery import shared_task

    @shared_task(name='cloud_mgr.run_issue_certificate')
    def run_issue_certificate(certificate_id, force=False):
        run_issue(certificate_id, force=force)

    @shared_task(name='cloud_mgr.run_deploy_certificate')
    def run_deploy_certificate(deployment_id):
        run_deploy(deployment_id)

    @shared_task(name='cloud_mgr.renew_expiring_certificates')
    def renew_expiring_certificates():
        """beat 每日调度：见 settings CELERY_BEAT_SCHEDULE。"""
        return renew_expiring()

except ImportError:  # celery 未安装时提供占位
    def run_issue_certificate(certificate_id, force=False):  # type: ignore
        raise RuntimeError('Celery 未安装，无法使用 celery 后端。请改用 thread/sync。')

    def run_deploy_certificate(deployment_id):  # type: ignore
        raise RuntimeError('Celery 未安装，无法使用 celery 后端。请改用 thread/sync。')
