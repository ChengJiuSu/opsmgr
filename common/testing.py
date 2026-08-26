"""测试辅助（仅供各 app 的 tests.py 使用）。"""


def grant_sudo(client, password='Pass!2345xyz'):
    """给已登录的测试客户端开一次 sudo 窗口。

    配置面（凭据/用户/Agent/通道路由/云账号/系统配置）的写操作要求二次验证，
    见 common/sudo.py。测试里账号都没启用 MFA，用密码确认即可。
    """
    resp = client.post('/api/auth/mfa/sudo/', {'password': password}, format='json')
    assert resp.status_code == 200, f'sudo 授权失败：{resp.status_code} {resp.content[:200]}'
    return client
