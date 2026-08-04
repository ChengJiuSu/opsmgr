"""JumpServer 注册方法执行器。

把模块匹配到的目标主机注册为 JumpServer 资产（复用 common.jumpserver，
与资产管理页「注册到 JumpServer」同一实现），供任务流在扩容/部署链路中
自动纳管新主机。幂等：已存在的资产跳过。

连接信息读系统配置：jumpserver.url / jumpserver.token（可选 org_id /
platform_id / node_id，详见 common.jumpserver 模块 docstring）。
方法无需专属配置（config 留空即可），目标主机即模块的主机范围。
"""
from common.jumpserver import register_servers

from .base import BaseExecutor, register


@register('jumpserver')
class JumpServerExecutor(BaseExecutor):

    def execute(self, task, method, inventory_path=None, servers=None, on_data=None):
        # task 为 TaskStep；注册走 JumpServer API，不使用 inventory_path/on_data
        servers = list(servers or [])
        if not servers:
            return 1, '没有可注册的目标主机。'
        try:
            results = register_servers(servers)
        except ValueError as exc:
            return 1, str(exc)

        lines = [f'[jumpserver] 注册目标：{len(servers)} 台']
        failed = 0
        for r in results:
            if not r['ok']:
                failed += 1
            lines.append(f'{"✔" if r["ok"] else "✖"} {r["name"]}({r["ip"]})：{r["msg"]}')
        lines.append(f'[jumpserver] 完成：成功 {len(results) - failed}，失败 {failed}')
        return (0 if failed == 0 else 1), '\n'.join(lines)
