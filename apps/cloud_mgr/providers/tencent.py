"""腾讯云 DNSPod 适配：requests + TC3-HMAC-SHA256 签名，不引入官方 SDK。

API 文档：https://cloud.tencent.com/document/product/1427
"""
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone

import requests

from .base import BaseDnsProvider, DnsProviderError, DnsRecordData

ENDPOINT = 'https://dnspod.tencentcloudapi.com'
SERVICE = 'dnspod'
API_VERSION = '2021-03-23'

# 平台内统一用英文线路码，DNSPod API 使用中文线路名，出入时转换
LINE_TO_CN = {'default': '默认', 'telecom': '电信', 'unicom': '联通', 'mobile': '移动', 'oversea': '境外'}
LINE_FROM_CN = {v: k for k, v in LINE_TO_CN.items()}


def _sign_tc3(secret_key, date, service, string_to_sign):
    k_date = hmac.new(('TC3' + secret_key).encode(), date.encode(), hashlib.sha256).digest()
    k_service = hmac.new(k_date, service.encode(), hashlib.sha256).digest()
    k_signing = hmac.new(k_service, b'tc3_request', hashlib.sha256).digest()
    return hmac.new(k_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()


class TencentDnsProvider(BaseDnsProvider):
    """腾讯云 DNSPod。"""

    def _request(self, action, params):
        payload = json.dumps(params)
        timestamp = int(time.time())
        date = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime('%Y-%m-%d')

        canonical_request = '\n'.join([
            'POST', '/', '',
            f'content-type:application/json\nhost:dnspod.tencentcloudapi.com\nx-tc-action:{action.lower()}\n',
            'content-type;host;x-tc-action',
            hashlib.sha256(payload.encode()).hexdigest(),
        ])
        credential_scope = f'{date}/{SERVICE}/tc3_request'
        string_to_sign = '\n'.join([
            'TC3-HMAC-SHA256', str(timestamp), credential_scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ])
        signature = _sign_tc3(self.secret_key, date, SERVICE, string_to_sign)
        headers = {
            'Content-Type': 'application/json',
            'Host': 'dnspod.tencentcloudapi.com',
            'X-TC-Action': action,
            'X-TC-Version': API_VERSION,
            'X-TC-Timestamp': str(timestamp),
            'Authorization': (
                f'TC3-HMAC-SHA256 Credential={self.access_key}/{credential_scope}, '
                f'SignedHeaders=content-type;host;x-tc-action, Signature={signature}'
            ),
        }
        try:
            resp = self.session.post(ENDPOINT, data=payload, headers=headers, timeout=15)
            data = resp.json().get('Response', {})
        except requests.RequestException as e:
            raise DnsProviderError(f'腾讯云 DNSPod API 请求失败：{e}') from e
        except ValueError as e:
            raise DnsProviderError('腾讯云 DNSPod API 返回非 JSON 响应') from e
        if 'Error' in data:
            err = data['Error']
            raise DnsProviderError(f"腾讯云 DNSPod API 错误：{err.get('Message', err.get('Code'))}")
        return data

    def list_domains(self):
        domains, offset = [], 0
        while True:
            data = self._request('DescribeDomainList', {'Offset': offset, 'Limit': 100})
            items = data.get('DomainList', [])
            domains.extend(d['Name'] for d in items)
            total = data.get('DomainCountInfo', {}).get('AllTotal', 0)
            offset += len(items)
            if not items or offset >= total:
                return domains

    def list_records(self, domain):
        records, offset = [], 0
        while True:
            data = self._request('DescribeRecordList', {
                'Domain': domain, 'Offset': offset, 'Limit': 1000,
            })
            items = data.get('RecordList', [])
            for r in items:
                records.append(DnsRecordData(
                    rr=r['Name'], type=r['Type'], value=r['Value'],
                    ttl=int(r.get('TTL', 600)),
                    line=LINE_FROM_CN.get(r.get('Line', '默认'), r.get('Line', 'default')),
                    remote_record_id=str(r['RecordId']),
                    enabled=r.get('Status') == 'ENABLE',
                ))
            total = data.get('RecordCountInfo', {}).get('TotalCount', 0)
            offset += len(items)
            if not items or offset >= total:
                return records

    def _line_cn(self, line):
        return LINE_TO_CN.get(line or 'default', line)

    def add_record(self, domain, record: DnsRecordData) -> str:
        params = {
            'Domain': domain, 'SubDomain': record.rr, 'RecordType': record.type,
            'Value': record.value, 'TTL': record.ttl,
            'RecordLine': self._line_cn(record.line),
        }
        if record.type == 'MX':
            params['MX'] = record.extra.get('priority', 10)
        data = self._request('CreateRecord', params)
        return str(data['RecordId'])

    def update_record(self, domain, record: DnsRecordData):
        params = {
            'Domain': domain, 'RecordId': int(record.remote_record_id),
            'SubDomain': record.rr, 'RecordType': record.type,
            'Value': record.value, 'TTL': record.ttl,
            'RecordLine': self._line_cn(record.line),
        }
        if record.type == 'MX':
            params['MX'] = record.extra.get('priority', 10)
        self._request('ModifyRecord', params)

    def delete_record(self, domain, remote_record_id: str):
        self._request('DeleteRecord', {'Domain': domain, 'RecordId': int(remote_record_id)})

    def set_record_enabled(self, domain, remote_record_id: str, enabled: bool):
        self._request('ModifyRecordStatus', {
            'Domain': domain, 'RecordId': int(remote_record_id),
            'Status': 'ENABLE' if enabled else 'DISABLE',
        })
