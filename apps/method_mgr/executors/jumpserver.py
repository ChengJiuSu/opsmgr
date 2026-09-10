"""JumpServer 注册方法执行器。

把模块匹配到的目标主机注册为 JumpServer 资产（复用 common.jumpserver，
与资产管理页「注册到 JumpServer」同一实现），供任务流在扩容/部署链路中
自动纳管新主机。幂等：已存在的资产跳过。

执行通道两条路（与 jenkins 方法同一套解析设施）：

- **直连**（manager 与 JumpServer 网络可达）：连接信息读系统配置
  jumpserver.url / jumpserver.token（可选 org_id / platform_id / node_id，
  详见 common.jumpserver 模块 docstring）；
- **经 Agent**（manager 在公网、JumpServer 在内网）：打成 step_type='jumpserver'
  的执行单元下发 agent，凭据只在 agent 本地 config.yaml 维护，不经通道下发。

通道解析（取第一个命中）：
    模块参数 params.agent → 方法配置 config.agent → 系统配置 jumpserver.agent
    → AgentRoute(模块自定义范围优先于任务维度) → 按每台主机自身的执行通道分组

最后一级意味着：任务里既有直连主机又有 NAT 内网主机时，各自走各自的通道，
无需为此拆成两个模块。方法无需专属配置（config 可留空），目标主机即模块的
主机范围。
"""
from common.jumpserver import channel_summary_lines, register_servers_routed

from .base import BaseExecutor, register


def _resolve_agent(step, config):
    """解析该 JumpServer 模块的统一执行通道，返回 (agent 或 None, source)。

    返回 None 表示没有统一通道，由 register_servers_routed 按每台主机的执行
    通道分组（不等于全部直连）。
    """
    from apps.agent_mgr.channel import SOURCE_METHOD, SOURCE_STEP
    from common.jumpserver import resolve_registration_agent

    scope = step if step.use_custom_scope else step.task
    return resolve_registration_agent(
        [(SOURCE_STEP, (step.params or {}).get('agent')),
         (SOURCE_METHOD, config.get('agent'))],
        project_group_id=scope.project_group_id,
        environment_id=scope.environment_id,
    )


@register('jumpserver')
class JumpServerExecutor(BaseExecutor):

    def validate_config(self, config):
        agent = (config or {}).get('agent')
        if agent is not None and not isinstance(agent, str):
            raise ValueError('config.agent 必须为 Agent 名称字符串')

    def execute(self, task, method, inventory_path=None, servers=None, on_data=None):
        # task 为 TaskStep；注册走 JumpServer API，不使用 inventory_path
        from apps.agent_mgr.channel import ChannelError

        step = task
        servers = list(servers or [])
        if not servers:
            return 1, '没有可注册的目标主机。'
        config = method.config or {}
        try:
            self.validate_config(config)
            agent, source = _resolve_agent(step, config)
        except (ChannelError, ValueError) as exc:
            return 1, str(exc)

        header = f'[jumpserver] 注册目标：{len(servers)} 台'
        if on_data:
            # 注册整段阻塞（agent 组等回传、直连组逐台 REST），先让人看见在跑什么
            on_data(header + '\n')
        try:
            results, channels = register_servers_routed(
                servers, agent=agent, source=source,
                task_id=step.task_id, step_order=step.order)
        except ChannelError as exc:
            return 1, str(exc)
        except ValueError as exc:  # 直连连接信息不完整 / 资产重名中止整批
            return 1, str(exc)

        lines = channel_summary_lines(channels) + [header]
        failed = sum(1 for r in results if not r['ok'])
        lines += [f'{"✔" if r["ok"] else "✖"} {r["name"]}({r["ip"]})：{r["msg"]}'
                  for r in results]
        lines.append(f'[jumpserver] 完成：成功 {len(results) - failed}，失败 {failed}')
        return (0 if failed == 0 else 1), '\n'.join(lines)
