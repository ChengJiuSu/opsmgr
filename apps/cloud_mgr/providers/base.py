"""DNS 服务商抽象基类与数据结构。"""
from dataclasses import dataclass, field

import requests


class DnsProviderError(Exception):
    """DNS 服务商操作失败（参数不支持、API 报错等），message 面向用户展示。"""


@dataclass
class DnsRecordData:
    """厂商侧解析记录的统一表示。"""

    rr: str
    type: str
    value: str
    ttl: int = 600
    line: str = 'default'
    remote_record_id: str = ''
    enabled: bool = True
    extra: dict = field(default_factory=dict)


class BaseDnsProvider:
    """DNS 服务商适配基类。所有方法失败时抛 DnsProviderError（中文信息）。"""

    def __init__(self, access_key, secret_key):
        self.access_key = access_key
        self.secret_key = secret_key
        # trust_env=False：不吃系统代理环境变量（WSL 下代理劫持公网 API 会
        # 超时/502，与 JumpServer 集成同坑同修法），云厂商公网端点直连
        self.session = requests.Session()
        self.session.trust_env = False

    def list_domains(self):
        """返回账号下托管的主域名列表：[str, ...]"""
        raise NotImplementedError

    def list_records(self, domain):
        """返回域名下全部解析记录：[DnsRecordData, ...]"""
        raise NotImplementedError

    def add_record(self, domain, record: DnsRecordData) -> str:
        """新增解析记录，返回厂商侧记录 ID。"""
        raise NotImplementedError

    def update_record(self, domain, record: DnsRecordData):
        """按 record.remote_record_id 更新解析记录。"""
        raise NotImplementedError

    def delete_record(self, domain, remote_record_id: str):
        """按厂商侧记录 ID 删除解析记录。"""
        raise NotImplementedError

    def set_record_enabled(self, domain, remote_record_id: str, enabled: bool):
        """启用/暂停解析记录；厂商不支持时抛 DnsProviderError。"""
        raise NotImplementedError
