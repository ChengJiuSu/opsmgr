"""阿里云解析 DNS（AliDNS）适配：requests + RPC 签名 V1，不引入官方 SDK。

API 文档：https://help.aliyun.com/zh/dns/api-alidns-2015-01-09-dir
"""
import base64
import hashlib
import hmac
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

import requests

from .base import BaseDnsProvider, DnsProviderError, DnsRecordData

ENDPOINT = 'https://alidns.aliyuncs.com'
API_VERSION = '2015-01-09'


def _percent_encode(s):
    return quote(str(s), safe='~')


class AliyunDnsProvider(BaseDnsProvider):
    """阿里云 DNS。解析线路 line 使用阿里云原生取值（default/telecom/unicom 等）。"""

    def _request(self, action, params):
        common = {
            'Format': 'JSON',
            'Version': API_VERSION,
            'AccessKeyId': self.access_key,
            'SignatureMethod': 'HMAC-SHA1',
            'SignatureVersion': '1.0',
            'SignatureNonce': uuid.uuid4().hex,
            'Timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'Action': action,
        }
        all_params = {**common, **{k: v for k, v in params.items() if v is not None}}
        query = '&'.join(
            f'{_percent_encode(k)}={_percent_encode(v)}'
            for k, v in sorted(all_params.items())
        )
        string_to_sign = 'GET&%2F&' + _percent_encode(query)
        signature = base64.b64encode(
            hmac.new((self.secret_key + '&').encode(), string_to_sign.encode(), hashlib.sha1).digest()
        ).decode()
        all_params['Signature'] = signature
        try:
            resp = self.session.get(ENDPOINT, params=all_params, timeout=15)
            data = resp.json()
        except requests.RequestException as e:
            raise DnsProviderError(f'阿里云 DNS API 请求失败：{e}') from e
        except ValueError as e:
            raise DnsProviderError('阿里云 DNS API 返回非 JSON 响应') from e
        if resp.status_code != 200 or 'Code' in data and data.get('Code'):
            msg = data.get('Message') or data.get('Code') or f'HTTP {resp.status_code}'
            raise DnsProviderError(f'阿里云 DNS API 错误：{msg}')
        return data

    def list_domains(self):
        domains, page = [], 1
        while True:
            data = self._request('DescribeDomains', {'PageNumber': page, 'PageSize': 100})
            items = data.get('Domains', {}).get('Domain', [])
            domains.extend(d['DomainName'] for d in items)
            if page * 100 >= int(data.get('TotalCount', 0)):
                return domains
            page += 1

    def list_records(self, domain):
        records, page = [], 1
        while True:
            data = self._request('DescribeDomainRecords', {
                'DomainName': domain, 'PageNumber': page, 'PageSize': 500,
            })
            items = data.get('DomainRecords', {}).get('Record', [])
            for r in items:
                records.append(DnsRecordData(
                    rr=r['RR'], type=r['Type'], value=r['Value'],
                    ttl=int(r.get('TTL', 600)), line=r.get('Line', 'default'),
                    remote_record_id=str(r['RecordId']),
                    enabled=r.get('Status') == 'ENABLE',
                ))
            if page * 500 >= int(data.get('TotalCount', 0)):
                return records
            page += 1

    def add_record(self, domain, record: DnsRecordData) -> str:
        params = {
            'DomainName': domain, 'RR': record.rr, 'Type': record.type,
            'Value': record.value, 'TTL': record.ttl, 'Line': record.line or 'default',
        }
        if record.type == 'MX':
            params['Priority'] = record.extra.get('priority', 10)
        data = self._request('AddDomainRecord', params)
        return str(data['RecordId'])

    def update_record(self, domain, record: DnsRecordData):
        params = {
            'RecordId': record.remote_record_id, 'RR': record.rr, 'Type': record.type,
            'Value': record.value, 'TTL': record.ttl, 'Line': record.line or 'default',
        }
        if record.type == 'MX':
            params['Priority'] = record.extra.get('priority', 10)
        self._request('UpdateDomainRecord', params)

    def delete_record(self, domain, remote_record_id: str):
        self._request('DeleteDomainRecord', {'RecordId': remote_record_id})

    def set_record_enabled(self, domain, remote_record_id: str, enabled: bool):
        self._request('SetDomainRecordStatus', {
            'RecordId': remote_record_id,
            'Status': 'Enable' if enabled else 'Disable',
        })
