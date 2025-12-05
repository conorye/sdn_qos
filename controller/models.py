# controller/models.py
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional
import time


@dataclass
class Flow:
    """业务流模型：控制器视角的一条流"""
    id: int
    src_ip: str
    dst_ip: str
    src_port: int 
    dst_port: int 
    request_rate_bps: int
    size_bytes: int
    priority: int  # 0=best, 1=silver, 2=gold
    reason : str
    
    # 调度结果
    send_rate_bps: int = 0
    dscp: Optional[int] = None
    queue_id: Optional[int] = None
    path: List[Tuple[int, int]] = field(default_factory=list)  # [(dpid, out_port), ...]

    # 状态
    
    status: str = "pending"  # pending/allowed/active/finished/failed
    created_at: float = field(default_factory=time.time)
    allowed_at: Optional[float] = None
    finished_at: Optional[float] = None
    
    # 统计信息：每个 hop 的字节数/速率
    hop_bytes: Dict[int, int] = field(default_factory=dict)      # dpid -> bytes
    hop_last_time: Dict[int, float] = field(default_factory=dict)
    hop_rate_bps: Dict[int, int] = field(default_factory=dict)

    # 逐跳释放相关
    released_hops: set = field(default_factory=set)


@dataclass
class PortState:
    """端口带宽预留状态"""
    dpid: int
    port_no: int
    capacity_bps: int
    reserved_total_bps: int = 0
    reserved_gold_bps: int = 0
    reserved_silver_bps: int = 0
    reserved_best_bps: int = 0

    def can_reserve(self, needed_bps: int) -> bool:
        return self.reserved_total_bps + needed_bps <= self.capacity_bps

    def reserve(self, bps: int, priority: int):
        self.reserved_total_bps += bps
        if priority == 2:
            self.reserved_gold_bps += bps
        elif priority == 1:
            self.reserved_silver_bps += bps
        else:
            self.reserved_best_bps += bps

    def release(self, bps: int, priority: int):
        self.reserved_total_bps = max(0, self.reserved_total_bps - bps)
        if priority == 2:
            self.reserved_gold_bps = max(0, self.reserved_gold_bps - bps)
        elif priority == 1:
            self.reserved_silver_bps = max(0, self.reserved_silver_bps - bps)
        else:
            self.reserved_best_bps = max(0, self.reserved_best_bps - bps)
