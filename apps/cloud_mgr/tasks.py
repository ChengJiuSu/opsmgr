"""证书签发/续签调度。

- run_issue：同步执行完整签发流程并落库（状态 issuing → issued/failed）；
- dispatch_issue：按 TASK_EXEC_BACKEND 调度（thread 默认 / celery / sync），
  与 task_exec 同口径；
- renew_expiring_certificates：celery beat 每日任务，auto_renew 且
  距过期不足 CERT_RENEW_BEFORE_DAYS 天的已签发证书强制重签。
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.db import connection
from django.utils import timezone

from .acme import AcmeError, issue_certificate

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

    @shared_task(name='cloud_mgr.renew_expiring_certificates')
    def renew_expiring_certificates():
        """beat 每日调度：见 settings CELERY_BEAT_SCHEDULE。"""
        return renew_expiring()

except ImportError:  # celery 未安装时提供占位
    def run_issue_certificate(certificate_id, force=False):  # type: ignore
        raise RuntimeError('Celery 未安装，无法使用 celery 后端。请改用 thread/sync。')
