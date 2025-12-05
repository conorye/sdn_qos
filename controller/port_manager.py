'''
Author: yc && qq747339545@163.com
Date: 2025-11-25 09:51:05
LastEditTime: 2025-11-28 17:51:04
FilePath: /sdn_qos/controller/port_manager.py
Description: DSCP 管理模块

Copyright (c) 2025 by ${git_name_email}, All Rights Reserved. 
'''
# controller/dscp_manager.py
from typing import Set, Tuple, Optional
from dataclasses import dataclass, field
from typing import Dict, Tuple


class DSCPManager:
    """
    管理 DSCP 分配：
    - priority 0: best-effort，默认范围 0-15
    - priority 1: silver，默认范围 16-31
    - priority 2: gold，默认范围 32-47
    """
    def __init__(self,):

        self.used: Set[int] = set()

    def alloc_dscp(self, priority: int) -> int:
        if priority == 2:
            dscp = 32
        elif priority == 1:
            dscp = 16
        else:
            dscp = 0
        # 不一定非要用 used，但保留接口
        self.used.add(dscp)
        return dscp

    def free_dscp(self, dscp: int):
        # 现在只是给你留个口子，真要做限流可以扩展
        self.used.discard(dscp)

    # def _get_range(self, priority: int) -> Tuple[int, int]:
    #     if priority == 2:
    #         return self.gold_range
    #     elif priority == 1:
    #         return self.silver_range
    #     else:
    #         return self.best_range

    # def alloc_dscp(self, priority: int) -> int:
    #     low, high = self._get_range(priority)
    #     for dscp in range(low, high + 1):
    #         if dscp not in self.used:
    #             self.used.add(dscp)
    #             return dscp
    #     # 分配不到就硬来：直接复用 low（极端情况下）
    #     # 你可以改成抛异常/拒绝调度
    #     # 在返回low之前添加错误检查
    #         if low > high:
    #             raise ValueError(f"Invalid DSCP range for priority {priority}")
    #         raise ValueError(f"No available DSCP in range {low}-{high} for priority {priority}")
                    
    #     return low

    # def free_dscp(self, dscp: int):
    #     self.used.discard(dscp)
# controller/port_manager.py
from dataclasses import dataclass, field
from typing import Dict, Tuple, Set, Optional

FlowKey = Tuple[str, int, str, int]  # (src_ip, src_port, dst_ip, dst_port)


@dataclass
class PortManager:
    """
    负责给发流分配 src_port / dst_port，并维护 4 元组到 flow_id 的映射。

    约定：
    - 发流使用的端口范围：[base_port, max_port]
    - 会自动跳过 reserved_ports（例如 Ryu 内部 TCP server、REST、host permit 口）
    """
    base_port: int = 20000
    max_port: int = 40000

    # Ryu 相关的已知端口，不参与发流端口分配，避免冲突
    # tcp_server_host: 172.17.0.1
    tcp_server_port: int = 9000
    rest_port: int = 8080
    host_permit_port: int = 10000

    _next_port: int = field(default=20000, init=False)
    reserved_ports: Set[int] = field(default_factory=set, init=False)

    # 只在 controller 内部保存完整映射，方便调试/查表
    flow_by_4tuple: Dict[FlowKey, int] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        # 把 Ryu 用到的端口配置为保留端口
        self.reserved_ports.update(
            {self.tcp_server_port, self.rest_port, self.host_permit_port}
        )

    # ----------------- 端口分配内部函数 -----------------

    def _alloc_port(self) -> int:
        """
        简单轮询的端口分配器：
        - 从 [base_port, max_port] 中轮询
        - 自动跳过 reserved_ports
        - 不做“是否已被系统占用”的检查（实验环境够用）
        """
        start = self._next_port

        while True:
            port = self._next_port
            self._next_port += 1
            if self._next_port > self.max_port:
                self._next_port = self.base_port

            # 跳过保留端口
            if port in self.reserved_ports:
                # 如果绕了一圈还没找到，说明可用端口已经耗尽
                if self._next_port == start:
                    raise RuntimeError("No available ports in the configured range")
                continue

            return port

    # ----------------- 对外 API：分配 src / dst 端口 -----------------

    def alloc_src_port(self, src_ip: str) -> int:
        """
        为某个 src_ip 分配一个 src_port。
        当前实现对 src_ip 不做区分，如果以后要做到 per-host 池，可以扩展。
        """
        return self._alloc_port()

    def alloc_dst_port(self, dst_ip: str) -> int:
        """
        为某个 dst_ip 分配一个 dst_port。
        同样只做简单轮询，避免和 reserved_ports 冲突。
        """
        return self._alloc_port()

    def alloc_flow_ports(
        self,
        src_ip: str,
        dst_ip: str,
        fixed_dst_port: Optional[int] = None,
    ) -> Tuple[int, int]:
        """
        一次性为“发流”分配 (src_port, dst_port)。

        - src_port 总是从可用端口池中分配
        - dst_port：
            * 如果传入 fixed_dst_port，则直接使用（适用于 9000 / 8080 / 10000 等固定端口）
            * 否则从可用端口池中再分配一个

        用法示例：
            # 发流到 Ryu TCP server:
            src_port, dst_port = pm.alloc_flow_ports(
                src_ip=host_ip,
                dst_ip="172.17.0.1",
                fixed_dst_port=pm.tcp_server_port,
            )

            # 发流到 host permit 口:
            src_port, dst_port = pm.alloc_flow_ports(
                src_ip=host_ip,
                dst_ip=host_ip,
                fixed_dst_port=pm.host_permit_port,
            )
        """
        src_port = self.alloc_src_port(src_ip)

        if fixed_dst_port is not None:
            dst_port = fixed_dst_port
        else:
            dst_port = self.alloc_dst_port(dst_ip)

        return src_port, dst_port

    # ----------------- 流绑定 / 查询 -----------------

    def bind_flow(
        self,
        flow_id: int,
        src_ip: str, src_port: int,
        dst_ip: str, dst_port: int,
    ) -> FlowKey:
        """
        把 4 元组和 flow_id 绑定，方便后续查表。
        """
        key = (src_ip, src_port, dst_ip, dst_port)
        self.flow_by_4tuple[key] = flow_id
        return key

    def get_flow_id(
        self,
        src_ip: str, src_port: int,
        dst_ip: str, dst_port: int,
    ) -> Optional[int]:
        """
        根据 4 元组查 flow_id，不存在则返回 None。
        """
        return self.flow_by_4tuple.get((src_ip, src_port, dst_ip, dst_port))
