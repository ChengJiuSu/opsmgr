"""MFA 业务逻辑：绑定、激活、校验、恢复码、解绑。视图只管参数与响应。

绑定分两步（setup → activate）：setup 落一条未启用记录并返回密钥与恢复码，
只有 activate 用一个真实验证码验过才真正启用。合成一步的代价是用户扫错码
或时钟不对就把自己锁在门外，只能找管理员。

强制范围读系统配置 `mfa.enforce_scope`（off / staff / all，默认 off）：能力
先上线、开关后拉，避免部署当天把所有人挡在门外。
"""
import hashlib
import logging
import secrets

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from common import totp
from common.crypto import DecryptError
from common.timeutil import local_dt

from .models import MFARecoveryCode, UserMFA

logger = logging.getLogger(__name__)

# 恢复码字符集去掉易混字符（0/O、1/I/L）：这串是要人手抄或手输的
RECOVERY_ALPHABET = '23456789ABCDEFGHJKMNPQRSTUVWXYZ'
RECOVERY_GROUPS = 3
RECOVERY_GROUP_LEN = 4  # 3×4 = 12 位，约 60 bit 熵

# 强制范围取值
SCOPE_OFF = 'off'
SCOPE_STAFF = 'staff'
SCOPE_ALL = 'all'
SCOPES = (SCOPE_OFF, SCOPE_STAFF, SCOPE_ALL)

# token 里的认证方式声明（RFC 8176 amr）：pwd = 只过了密码，otp = 过了第二因子
AMR_PASSWORD = 'pwd'
AMR_OTP = 'otp'


class MfaError(Exception):
    """面向使用者的失败原因（视图直接把 message 返回）。"""


# ── 强制策略 ──────────────────────────────────────────────

def enforce_scope():
    """当前强制范围；配置值非法时按 off 处理并告警。

    宁可「没强制」也不要因为一个拼错的配置值把全员挡在门外——真正的兜底是
    告警 + 文档，不是在鉴权路径上抛异常。
    """
    from apps.system_cfg.services import get_config

    scope = (get_config('mfa.enforce_scope', SCOPE_OFF) or SCOPE_OFF).lower()
    if scope not in SCOPES:
        logger.warning('系统配置 mfa.enforce_scope=%r 非法，按 %s 处理（可选：%s）',
                       scope, SCOPE_OFF, '/'.join(SCOPES))
        return SCOPE_OFF
    return scope


def required_for(user):
    """该用户是否被强制绑定 MFA。"""
    scope = enforce_scope()
    if scope == SCOPE_ALL:
        return True
    return scope == SCOPE_STAFF and bool(user.is_staff)


# ── 状态查询 ──────────────────────────────────────────────

def get_binding(user):
    return UserMFA.objects.filter(user=user).first()


def is_enabled(user):
    binding = get_binding(user)
    return bool(binding and binding.enabled)


def status(user):
    binding = get_binding(user)
    return {
        'enabled': bool(binding and binding.enabled),
        'pending': bool(binding and not binding.enabled),
        'activated_at': local_dt(binding.activated_at) if binding else None,
        'last_used_at': local_dt(binding.last_used_at) if binding else None,
        'recovery_codes_left': MFARecoveryCode.objects.filter(
            user=user, used_at__isnull=True).count(),
        'enforced': required_for(user),
        'enforce_scope': enforce_scope(),
        # 敏感操作二次验证窗口秒数，供前端提示「验过多久内免重验」
        'sudo_ttl': settings.SUDO_TTL,
    }


# ── 绑定流程 ──────────────────────────────────────────────

@transaction.atomic
def start_enrollment(user):
    """（重新）开始绑定：换新密钥与新恢复码，返回一次性明文。

    已启用的绑定不允许覆盖——否则拿到别人已登录会话的人可以悄悄把 MFA 换到
    自己的验证器上。要换设备先 disable（需密码 + 当前验证码）。
    """
    binding = get_binding(user)
    if binding and binding.enabled:
        raise MfaError('已绑定 MFA。要换设备请先解绑（需密码与当前验证码）。')

    secret = totp.random_secret()
    if binding is None:
        binding = UserMFA(user=user)
    binding.set_secret(secret)
    binding.enabled = False
    binding.activated_at = None
    binding.last_counter = 0
    binding.save()
    codes = _issue_recovery_codes(user)
    return {
        'secret': secret,
        'uri': totp.provisioning_uri(secret, user.username, settings.MFA_ISSUER),
        'recovery_codes': codes,
    }


@transaction.atomic
def activate(user, code):
    """用一个正确验证码启用绑定。"""
    binding = get_binding(user)
    if binding is None:
        raise MfaError('尚未开始绑定，请先获取二维码。')
    if binding.enabled:
        raise MfaError('MFA 已启用。')
    counter = _verify_totp(binding, code)
    if counter is None:
        raise MfaError('验证码不正确。请确认手机时间准确，并使用当前显示的 6 位码。')
    binding.enabled = True
    binding.activated_at = timezone.now()
    binding.last_counter = counter
    binding.last_used_at = timezone.now()
    binding.save(update_fields=['enabled', 'activated_at', 'last_counter',
                                'last_used_at', 'updated_at'])
    logger.info('用户 %s 启用 MFA', user.username)


@transaction.atomic
def disable(user):
    """解绑并清空恢复码（调用方负责校验密码与当前验证码）。"""
    UserMFA.objects.filter(user=user).delete()
    MFARecoveryCode.objects.filter(user=user).delete()
    logger.warning('用户 %s 解绑 MFA', user.username)


@transaction.atomic
def reset(user):
    """管理员/命令行重置：清绑定，用户下次登录重新绑。"""
    disable(user)


# ── 校验 ──────────────────────────────────────────────────

def verify(user, code):
    """校验验证码或恢复码，返回 'totp' / 'recovery'；失败抛 MfaError。"""
    binding = get_binding(user)
    if binding is None or not binding.enabled:
        raise MfaError('该账号未启用 MFA。')

    code = str(code or '').strip()
    if not code:
        raise MfaError('请输入验证码。')

    counter = _verify_totp(binding, code)
    if counter is not None:
        UserMFA.objects.filter(pk=binding.pk).update(
            last_counter=counter, last_used_at=timezone.now(),
            updated_at=timezone.now())
        return 'totp'

    if _consume_recovery_code(user, code):
        left = MFARecoveryCode.objects.filter(user=user, used_at__isnull=True).count()
        logger.warning('用户 %s 使用恢复码登录，剩余 %d 个', user.username, left)
        return 'recovery'

    if _is_replayed(binding, code):
        # 码本身是对的，只是这个 30 秒窗口已经用过（登录刚验过 / 表单重复提交）。
        # 说成「不正确」会让人反复怀疑密钥或时钟，实际只需等下一个码
        raise MfaError('该验证码已使用过，请等验证器换出下一个 6 位码再试。')
    raise MfaError('验证码不正确。')


def _is_replayed(binding, code):
    """码正确但所属时间窗已用过（防重放拒的那种），用于给出准确文案。"""
    try:
        secret = binding.get_secret()
    except DecryptError:
        return False
    return totp.verify(secret, code) is not None


def _verify_totp(binding, code):
    """返回命中的时间窗计数器；失败或密钥解不开返回 None。"""
    try:
        secret = binding.get_secret()
    except DecryptError:
        logger.error('用户 %s 的 MFA 密钥无法解密（MFA_SECRET_KEY 变更？），需重置绑定',
                     binding.user_id)
        return None
    # after_counter 防重放：同一个 30 秒窗口的码用过一次就不再认
    return totp.verify(secret, code, after_counter=binding.last_counter or None)


# ── 恢复码 ────────────────────────────────────────────────

def _hash_code(code):
    """恢复码哈希。

    用 sha256 而非 PBKDF2：恢复码是平台生成的 12 位随机串（约 60 bit 熵），
    不存在字典攻击面，而慢哈希无法按哈希值查库，只能逐条比对——10 条 PBKDF2
    会给登录路径白加几百毫秒。
    """
    return hashlib.sha256(normalize_recovery_code(code).encode('utf-8')).hexdigest()


def normalize_recovery_code(code):
    """去分隔符与空格并转大写：抄下来的码带不带横线都认。"""
    return str(code or '').strip().upper().replace('-', '').replace(' ', '')


def _new_recovery_code():
    groups = [
        ''.join(secrets.choice(RECOVERY_ALPHABET) for _ in range(RECOVERY_GROUP_LEN))
        for _ in range(RECOVERY_GROUPS)
    ]
    return '-'.join(groups)


def _issue_recovery_codes(user):
    """作废旧码、发一批新码，返回明文（仅此一次可见）。"""
    MFARecoveryCode.objects.filter(user=user).delete()
    codes = [_new_recovery_code() for _ in range(settings.MFA_RECOVERY_CODES)]
    MFARecoveryCode.objects.bulk_create([
        MFARecoveryCode(user=user, code_hash=_hash_code(code)) for code in codes
    ])
    return codes


@transaction.atomic
def regenerate_recovery_codes(user):
    """重新生成恢复码（调用方负责校验密码与当前验证码）。"""
    if not is_enabled(user):
        raise MfaError('该账号未启用 MFA。')
    return _issue_recovery_codes(user)


def _consume_recovery_code(user, code):
    """核销一个恢复码，成功返回 True。

    `update(...)` 带 `used_at__isnull=True` 条件，靠数据库保证并发下同一个码
    只会被核销一次。
    """
    normalized = normalize_recovery_code(code)
    if len(normalized) != RECOVERY_GROUPS * RECOVERY_GROUP_LEN:
        return False
    updated = MFARecoveryCode.objects.filter(
        user=user, code_hash=_hash_code(normalized), used_at__isnull=True,
    ).update(used_at=timezone.now())
    return bool(updated)
