'''
Author: yc && qq747339545@163.com
Date: 2025-11-25 09:50:56
LastEditTime: 2025-11-25 22:27:21
FilePath: /sdn_qos/controller/admission_control.py
Description: Admission 控制模块

Copyright (c) 2025 by ${git_name_email}, All Rights Reserved. 
'''
# controller/admission_control.py
from typing import Dict, Tuple, List
from models import PortState, Flow


class AdmissionControl:
    """
    控制器侧的带宽预留账本 + Admission 判断。
    """

    def __init__(self, port_capacity: Dict[Tuple[int, int], int]):
        """
        port_capacity: (dpid, port_no) -> capacity_bps
        """
        self.ports: Dict[Tuple[int, int], PortState] = {}
        for (dpid, port), cap in port_capacity.items():
            self.ports[(dpid, port)] = PortState(dpid=dpid, port_no=port, capacity_bps=cap)

    def get_port_state(self, dpid: int, port_no: int) -> PortState:
        return self.ports[(dpid, port_no)]

    def can_admit(self, flow: Flow, path: List[Tuple[int, int]]) ->  Tuple[bool, int]:
        """
        判断是否能接纳这条流。
        返回 (ok, send_rate_bps)。
        当前版本简单：send_rate_bps = request_rate_bps，
        若有任何 hop 的可用带宽 < request_rate_bps，则不允许。
        你可以在这里做更复杂的瓶颈计算。
        """
        req = flow.request_rate_bps

        for dpid, port in path:
            ps = self.ports.get((dpid, port))
            if ps is None:
                # 未配置的端口，视为不可用
                return False, 0
            if not ps.can_reserve(req):
                return False, 0
        return True, req

    def reserve(self, flow: Flow, path: List[Tuple[int, int]]):
        """在路径上的每个端口预留带宽"""
        for dpid, port in path:
            ps = self.ports[(dpid, port)]
            ps.reserve(flow.send_rate_bps, flow.priority)

    def release(self, flow: Flow):
        """释放整条路径上的预留（适用于流结束）"""
        if not flow.path:
            return
        for dpid, port in flow.path:
            ps = self.ports.get((dpid, port))
            if not ps:
                continue
            ps.release(flow.send_rate_bps, flow.priority)

    def release_single_port(self, dpid: int, port_no: int, flow: Flow):
        """逐跳释放：只释放一个端口的预留"""
        ps = self.ports.get((dpid, port_no))
        if ps:
            ps.release(flow.send_rate_bps, flow.priority)
            
    def dump_book(self) -> List[Tuple[int, int, int, int, int]]:
        """返回所有端口的带宽信息 (dpid, port, capacity, reserved_total, available)"""
        result = []
        for (dpid, port), ps in self.ports.items():
            # 关键修正：使用 reserved_total_bps 作为总预留带宽
            # 计算可用带宽 = capacity - reserved_total
            available_bps = ps.capacity_bps - ps.reserved_total_bps
            
            result.append((
                dpid,
                port,
                ps.capacity_bps,       # 总带宽
                ps.reserved_total_bps,  # 总预留带宽（关键！）
                available_bps           # 可用带宽（计算得出）
            ))
        return result
