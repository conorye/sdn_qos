'''
Author: yc && qq747339545@163.com
Date: 2025-11-25 09:51:05
LastEditTime: 2025-11-25 16:54:20
FilePath: /sdn_qos/controller/dscp_manager.py
Description: DSCP 管理模块

Copyright (c) 2025 by ${git_name_email}, All Rights Reserved. 
'''
# controller/dscp_manager.py
from typing import Set, Tuple, Optional


class DSCPManager:
    """
    管理 DSCP 分配：
    - priority 0: best-effort，默认范围 0-15
    - priority 1: silver，默认范围 16-31
    - priority 2: gold，默认范围 32-47
    """
    def __init__(
        self,
        best_range: Tuple[int, int] = (0, 15), # Best-Effort
        silver_range: Tuple[int, int] = (16, 31),  # Silver 
        gold_range: Tuple[int, int] = (32, 47), # Gold
    ):
        self.best_range = best_range
        self.silver_range = silver_range
        self.gold_range = gold_range
        self.used: Set[int] = set()

    def _get_range(self, priority: int) -> Tuple[int, int]:
        if priority == 2:
            return self.gold_range
        elif priority == 1:
            return self.silver_range
        else:
            return self.best_range

    def alloc_dscp(self, priority: int) -> int:
        low, high = self._get_range(priority)
        for dscp in range(low, high + 1):
            if dscp not in self.used:
                self.used.add(dscp)
                return dscp
        # 分配不到就硬来：直接复用 low（极端情况下）
        # 你可以改成抛异常/拒绝调度
        # 在返回low之前添加错误检查
            if low > high:
                raise ValueError(f"Invalid DSCP range for priority {priority}")
            # raise ValueError(f"No available DSCP in range {low}-{high} for priority {priority}")
                    
        return low

    def free_dscp(self, dscp: int):
        self.used.discard(dscp)
