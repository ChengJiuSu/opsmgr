"""DNS 服务商适配层。

按 CloudProvider.code 注册实现类；新厂商实现 BaseDnsProvider 后
在 PROVIDER_REGISTRY 登记即可接入。
"""
from .base import BaseDnsProvider, DnsProviderError, DnsRecordData
from .aliyun import AliyunDnsProvider
from .tencent import TencentDnsProvider

# key 为 CloudProvider.code（字典表编码）
PROVIDER_REGISTRY = {
    'aliyun': AliyunDnsProvider,
    'tencent': TencentDnsProvider,
}


def get_dns_provider(cloud_account):
    """按云账号所属厂商 code 取 DNS 适配实例；未支持的厂商抛 DnsProviderError。"""
    code = cloud_account.provider.code
    cls = PROVIDER_REGISTRY.get(code)
    if cls is None:
        raise DnsProviderError(f'云厂商 {cloud_account.provider.name}({code}) 暂不支持 DNS 管理，'
                               f'当前支持：{"、".join(PROVIDER_REGISTRY)}')
    return cls(cloud_account.access_key, cloud_account.secret_key)
