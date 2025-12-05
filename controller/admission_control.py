'''
Author: yc && qq747339545@163.com
Date: 2025-11-25 09:50:56
LastEditTime: 2025-12-01 15:07:13
FilePath: /sdn_qos/controller/admission_control.py
Description: Admission 控制模块

Copyright (c) 2025 by ${git_name_email}, All Rights Reserved. 
'''
# controller/admission_control.py
from typing import Dict, Tuple, List
from models import PortState, Flow
import os
import time

class AdmissionControl:
    """
    控制器侧的带宽预留账本 + Admission 判断。
    """

    def __init__(self, port_capacity: Dict[Tuple[int, int], int],log_root: str):
        """
        port_capacity: (dpid, port_no) -> capacity_bps
        """
        self.ports: Dict[Tuple[int, int], PortState] = {}
        for (dpid, port), cap in port_capacity.items():
            self.ports[(dpid, port)] = PortState(dpid=dpid, port_no=port, capacity_bps=cap)
            
        # --- 日志目录 ---
        self.log_root = log_root or "/home/yc/sdn_qos/logs"
        
        # 1) 每条流的 PortState 变更日志：Flow_PortState/<flow_id>.log
        self.port_log_root = os.path.join(self.log_root, "Flow_PortState")
        os.makedirs(self.port_log_root, exist_ok=True)

        # 2) 全局端口快照日志：PortSnapshot/port_snapshot.log
        self.port_snapshot_dir = os.path.join(self.log_root, "PortSnapshot")
        os.makedirs(self.port_snapshot_dir, exist_ok=True)
        self.port_snapshot_log_path = os.path.join(
            self.port_snapshot_dir, "port_snapshot.log"
        )

    # ----------------------------------------------------
    # 基础写文件工具
    # ----------------------------------------------------
    def _write(self, path: str, text: str):
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)

    # ----------------------------------------------------
    # Flow 级的 PortState 日志（Flow_PortState）
    # ----------------------------------------------------
    def _flow_log_path(self, flow_id: int) -> str:
        """每个 flow 单独一个日志文件"""
        return os.path.join(self.port_log_root, f"{flow_id}.log")

    def _log_reserve_path(self, flow: Flow, path: List[Tuple[int, int]]):
        """记录某条流在哪条路径上预留了多少带宽"""
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        path_str = " -> ".join(f"s{dpid}:{port}" for dpid, port in path)
        msg = (
            f"{ts} [ReservePath] flow={flow.id} class={flow.priority} "
            f"rate={flow.send_rate_bps} path={path_str}\n"
        )
        self._write(self._flow_log_path(flow.id), msg)

    def _log_port_change(self, flow: Flow, ps: PortState,
                         action: str, delta: int, before: int):
        """
        action: PortReserve / PortRelease / PortReleaseSingle
        delta : 这次变动的带宽
        before: 操作前 reserved_total_bps
        """
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        msg = (
            f"{ts} [{action}] flow={flow.id} class={flow.priority} "
            f"dpid={ps.dpid} port={ps.port_no} delta_bps={delta} "
            f"total_before={before} total_after={ps.reserved_total_bps} "
            f"gold={ps.reserved_gold_bps} silver={ps.reserved_silver_bps} "
            f"best={ps.reserved_best_bps}\n"
        )
        self._write(self._flow_log_path(flow.id), msg)

    # ----------------------------------------------------
    # 全局端口快照日志（PortSnapshot）
    # ----------------------------------------------------
    def log_port_snapshot(self, tag: str = ""):
        """
        把当前所有端口的状态打一个快照到：
          <log_root>/PortSnapshot/port_snapshot.log
        一般由 StatsCollector 周期性调用。
        """
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        header = f"{ts} [PortSnapshot]"
        if tag:
            header += f" tag={tag}"
        header += "\n"
        self._write(self.port_snapshot_log_path, header)

        # 排序一下，日志更好看
        for (dpid, port), ps in sorted(self.ports.items()):
            avail = ps.capacity_bps - ps.reserved_total_bps
            line = (
                f"  s{dpid}:{port} cap={ps.capacity_bps} "
                f"reserved={ps.reserved_total_bps} avail={avail} "
                f"gold={ps.reserved_gold_bps} "
                f"silver={ps.reserved_silver_bps} "
                f"best={ps.reserved_best_bps}\n"
            )
            self._write(self.port_snapshot_log_path, line)

    # ----------------------------------------------------
    # Admission 判断
    # ----------------------------------------------------

    def can_admit(self, flow: Flow, path: List[Tuple[int, int]]) -> Tuple[bool, int, str]:
        req = flow.request_rate_bps
        for dpid, port in path:
            ps = self.ports.get((dpid, port))
            if ps is None:
                return False, 0, "no_port"
            if not ps.can_reserve(req):
                return False, 0, "no_capacity"
        return True, req, "ok"

    def get_port_state(self, dpid: int, port_no: int) -> PortState:
        return self.ports[(dpid, port_no)]

    def can_admit(self, flow: Flow, path: List[Tuple[int, int]]) ->  Tuple[bool, int,str]:
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
                return False, 0,"no_path"
            if not ps.can_reserve(req):
                return False, 0 ,"no_capacity"
        return True, req ,"ok"

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
            
 # ----------------------------------------------------
    # dump_book（给 StatsCollector 原来的 _print_port_book 用）
    # ----------------------------------------------------
    def dump_book(self) -> List[Tuple[int, int, int, int, int]]:
        """返回所有端口的带宽信息 (dpid, port, capacity, reserved_total, available)"""
        result = []
        for (dpid, port), ps in self.ports.items():
            available_bps = ps.capacity_bps - ps.reserved_total_bps
            result.append((
                dpid,
                port,
                ps.capacity_bps,
                ps.reserved_total_bps,
                available_bps
            ))
        return result
