"""解析记录变更的高危判定。

不是所有 DNS 操作都值得同等对待：加一条新的测试子域名风险很低，改根域 A 记录
或删 MX 是另一个量级。全都拦会让人绕过流程（把审批当成障碍去想办法躲），全都
不拦等于没有防线——所以要分级，只对高危加闸。

高危 = 命中任一条：

1. **改 / 删已同步的记录** —— 新增不算：加一条新记录不会动到线上现有解析；
2. **根域 `@` 记录** —— 主域名本身的解析，影响面最大；
3. **NS / MX / CAA / TXT** —— NS 改错整个域名失控；MX 改错邮件静默丢失；
   CAA 影响证书签发；TXT 里躺着 SPF、DKIM 与各类域名所有权验证，改错的表现
   都是「过几天才发现」；
4. **被标记保护的记录或域名**（`protected`）—— 人工指定，同步时也会自动给
   `@`/NS/MX 打上；
5. **指向生产主机的记录** —— `target_type='server'` 且该主机环境 `is_prod`。

判定只看「这次要做什么」，不看「谁在做」：权限是另一层的事。
"""
#: 天然高危的记录类型（理由见模块说明）
HIGH_RISK_TYPES = frozenset({'NS', 'MX', 'CAA', 'TXT'})

#: 根域主机记录的写法
ROOT_RR = '@'


def auto_protected(rr, record_type):
    """同步/新建时是否自动打保护标记。

    只自动标最没有争议的那几类（根域、NS、MX）。TXT 数量多且大量是临时验证记录，
    自动标会让「保护」这个标记失去信号价值，所以不自动标——它仍然因类型命中高危。
    """
    return rr == ROOT_RR or record_type in ('NS', 'MX')


def risk_reasons(record, action, domain=None):
    """返回该次变更命中的高危理由列表；空列表 = 低危。

    record 可以是尚未入库的 DnsRecord 实例（新增/修改的目标值）。
    """
    domain = domain or getattr(record, 'domain', None)
    reasons = []

    if action in ('update', 'delete') and getattr(record, 'remote_record_id', ''):
        reasons.append('修改/删除已生效的线上解析')
    if getattr(record, 'rr', '') == ROOT_RR:
        reasons.append('根域 @ 记录')
    record_type = getattr(record, 'type', '')
    if record_type in HIGH_RISK_TYPES:
        reasons.append(f'{record_type} 记录（NS/MX/CAA/TXT 改错通常过几天才发现）')
    if getattr(record, 'protected', False):
        reasons.append('记录被标记为受保护')
    if domain is not None and getattr(domain, 'protected', False):
        reasons.append(f'域名 {domain.name} 被标记为受保护')
    if _points_to_prod(record):
        reasons.append('指向生产环境主机')
    return reasons


def is_high_risk(record, action, domain=None):
    return bool(risk_reasons(record, action, domain))


def _points_to_prod(record):
    if getattr(record, 'target_type', '') != 'server':
        return False
    server = getattr(record, 'server', None)
    environment = getattr(server, 'environment', None) if server else None
    return bool(environment and environment.is_prod)
