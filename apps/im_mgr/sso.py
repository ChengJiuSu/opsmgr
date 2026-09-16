"""飞书网页免登（SSO）。

浏览器跳飞书授权页 → 带 code 回到前端 → 前端把 code 交给后端 → 后端换
`user_access_token` 并读用户信息 → 落到平台 User → 签发 JWT。

几处关键取舍：

- **不需要为 SSO 额外开放公网**。授权码由用户浏览器带回，飞书服务器不访问回调
  地址；换 token 是平台的**出站**请求。所以回调地址是给浏览器用的前端路由，
  与 `/api/feishu/callback/` 那两条入站入口完全无关。
- **不自动建用户**（`auto_create_user` 默认关）。开了等于全公司飞书员工都能进
  后台——「谁能进平台」不该由通讯录决定。查不到对应账号就明确拒绝并提示找管理员。
- **邮箱自动匹配只在唯一命中时生效**。匹配到多个账号一律拒绝：这条路本来就是最
  容易绑错人的一条，宁可让管理员手工绑。
- **state 一次性**。后端预先签发、存缓存 5 分钟、用掉即删，防的是别人把自己的
  授权码塞给你的浏览器（登录 CSRF）。缓存不可用时拒绝登录（fail-closed）——
  这是登录路径，放行的后果是这道防护形同虚设。

签发的令牌 `amr=['sso']`，被 `MfaJWTAuthentication` 视为已过第二因子
（理由见 `apps/auth_mgr/services.AMR_STRONG`）。**sudo 窗口不吃这一条**：
绑了 TOTP 的用 TOTP，没绑的回退密码；自动创建的账号没有可用密码，因此做不了
配置面的写操作——这是刻意的，那类账号本就不该直接拿到配置权限。
"""
import logging
import secrets
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.utils import timezone

from . import client
from .models import FeishuApp, FeishuBinding

logger = logging.getLogger(__name__)

User = get_user_model()

STATE_PREFIX = 'feishu:sso:state:'
STATE_TTL = 300

#: 换 user_access_token。Authorization 里放的是 app_access_token
OIDC_TOKEN_PATH = '/open-apis/authen/v1/oidc/access_token'
#: 读登录用户信息（name / open_id / union_id / email）
USER_INFO_PATH = '/open-apis/authen/v1/user_info'
#: 默认前端回调路由（飞书后台「重定向 URL」白名单里要有同一个地址）
DEFAULT_REDIRECT_PATH = '/login/feishu'


class SsoError(Exception):
    """飞书登录失败（消息直接回给用户，因此必须是可行动的中文）。"""


def get_app():
    """取已启用飞书登录的配置。"""
    app = FeishuApp.load()
    if not app.is_ready():
        raise SsoError('飞书接入未启用或配置不完整，请联系管理员。')
    if not app.sso_enabled:
        raise SsoError('飞书登录未启用，请使用账号密码登录。')
    return app


def redirect_uri(app, request=None):
    """授权回调地址：配置了就用配置的，否则按当前请求域名推导。"""
    if app.sso_redirect_uri:
        return app.sso_redirect_uri
    if request is None:
        raise SsoError('未配置飞书登录回调地址，请联系管理员。')
    return request.build_absolute_uri(DEFAULT_REDIRECT_PATH)


def issue_state():
    """签发一次性 state 并存缓存；缓存不可用直接拒绝登录（fail-closed）。"""
    state = secrets.token_urlsafe(24)
    try:
        cache.set(STATE_PREFIX + state, 1, timeout=STATE_TTL)
    except Exception as exc:  # noqa: BLE001 - 缓存后端异常类型由实现决定
        logger.warning('飞书登录 state 写缓存失败', exc_info=True)
        raise SsoError('登录服务暂不可用（缓存故障），请稍后再试。') from exc
    return state


def consume_state(state):
    """消费 state；不存在或已用过即拒。

    用 `cache.delete` 的返回值判定「确实存在且由我删掉」，避免 get 之后再 delete
    的窗口里被并发用第二次。
    """
    if not state:
        raise SsoError('登录状态缺失，请重新发起飞书登录。')
    try:
        existed = cache.delete(STATE_PREFIX + str(state))
    except Exception as exc:  # noqa: BLE001
        logger.warning('飞书登录 state 校验失败（缓存故障）', exc_info=True)
        raise SsoError('登录服务暂不可用（缓存故障），请稍后再试。') from exc
    if not existed:
        raise SsoError('登录状态已失效，请重新发起飞书登录。')


def authorize_url(app, request=None):
    """拼授权跳转地址，返回 (url, state)。"""
    state = issue_state()
    query = urlencode({
        'app_id': app.app_id,
        'redirect_uri': redirect_uri(app, request),
        'response_type': 'code',
        'state': state,
    })
    base = getattr(settings, 'FEISHU_BASE_URL', 'https://open.feishu.cn').rstrip('/')
    path = getattr(settings, 'FEISHU_AUTHORIZE_PATH', '/open-apis/authen/v1/authorize')
    return f'{base}{path}?{query}', state


def exchange_code(app, code):
    """授权码 → user_access_token。"""
    data = client.call(
        'POST', OIDC_TOKEN_PATH,
        payload={'grant_type': 'authorization_code', 'code': code},
        app=app, token=client.get_app_token(app),
    )
    token = data.get('access_token')
    if not token:
        raise SsoError('飞书未返回用户令牌，请重试。')
    return token


def fetch_profile(app, user_access_token):
    """读飞书登录用户信息。"""
    profile = client.call('GET', USER_INFO_PATH, app=app, token=user_access_token)
    if not profile.get('union_id') and not profile.get('open_id'):
        raise SsoError('飞书未返回用户标识，请联系管理员检查应用权限。')
    return profile


def profile_email(profile):
    """取可用于匹配平台账号的邮箱：企业邮箱优先。

    个人邮箱是用户自己填的，拿它匹配平台账号等于让用户自选绑定对象。
    """
    return str(profile.get('enterprise_email') or profile.get('email') or '').strip()


def resolve_user(app, profile):
    """飞书身份 → 平台用户，返回 (user, binding)。"""
    union_id = str(profile.get('union_id') or '')
    open_id = str(profile.get('open_id') or '')
    binding = FeishuBinding.resolve(union_id=union_id, open_id=open_id)
    if binding is not None:
        return _check_usable(binding.user), binding

    email = profile_email(profile)
    user = _match_by_email(email)
    source = 'email'
    if user is None:
        if not app.auto_create_user:
            raise SsoError(
                f'飞书账号（{profile.get("name") or open_id}）未绑定平台用户，'
                f'请联系管理员在「用户管理」中绑定后再登录。')
        user = _create_user(profile, email)
        source = 'auto_create'

    if not union_id:
        # 没有 union_id 就建不了绑定（唯一键），而这通常意味着应用少了通讯录权限
        raise SsoError('飞书未返回 UnionID，请联系管理员检查应用的通讯录权限。')
    binding = FeishuBinding.objects.create(
        user=_check_usable(user), union_id=union_id, open_id=open_id,
        feishu_name=str(profile.get('name') or '')[:100],
        feishu_email=email[:254], source=source,
    )
    logger.info('飞书账号 %s 绑定到平台用户 %s（来源：%s）',
                union_id, user.username, source)
    return user, binding


def _check_usable(user):
    if not user.is_active:
        raise SsoError('该账号已停用，请联系管理员。')
    if user.is_service:
        # 服务账号是 API 凭证的行权身份，不是人，不能用来登录
        raise SsoError('该账号为服务账号，不允许登录。')
    return user


def _match_by_email(email):
    """按邮箱唯一匹配平台账号；匹配到多个一律放弃（宁可让管理员手工绑）。"""
    if not email:
        return None
    matched = list(User.objects.filter(
        email__iexact=email, is_service=False)[:2])
    if len(matched) != 1:
        if matched:
            logger.warning('飞书邮箱 %s 匹配到多个平台账号，放弃自动绑定', email)
        return None
    return matched[0]


def _create_user(profile, email):
    """自动创建平台账号（默认关闭；开启时也只给最小权限）。

    不设可用密码：这类账号只能经飞书登录，也因此做不了需要 sudo 的配置面写操作。
    不给角色：落到「无角色」分支（非 staff），即普通运维的只读矩阵、无执行能力。
    """
    base = (email.split('@')[0] if email else '') or f'feishu_{secrets.token_hex(4)}'
    username = base[:140]
    suffix = 1
    while User.objects.filter(username=username).exists():
        username = f'{base[:135]}_{suffix}'
        suffix += 1
    user = User.objects.create(
        username=username, email=email[:254],
        first_name=str(profile.get('name') or '')[:150],
        is_staff=False, is_active=True,
    )
    user.set_unusable_password()
    user.save(update_fields=['password'])
    logger.warning('飞书登录自动创建平台账号：%s（%s）', username, email or '无邮箱')
    return user


def login(app, code, state, request=None):
    """完整走一次飞书登录，返回 (user, 令牌 dict)。"""
    from apps.auth_mgr.tokens import issue_after_sso

    consume_state(state)
    if not code:
        raise SsoError('缺少飞书授权码，请重新发起登录。')

    token = exchange_code(app, code)
    profile = fetch_profile(app, token)
    user, binding = resolve_user(app, profile)

    binding.open_id = str(profile.get('open_id') or binding.open_id)
    binding.feishu_name = str(profile.get('name') or binding.feishu_name)[:100]
    binding.last_login_at = timezone.now()
    binding.save(update_fields=['open_id', 'feishu_name', 'last_login_at', 'updated_at'])
    return user, issue_after_sso(user)
