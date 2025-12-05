'''
Author: yc && qq747339545@163.com
Date: 2025-11-25 15:39:46
LastEditTime: 2025-11-28 11:42:25
FilePath: /sdn_qos/controller/exp_logger.py
Description: 

Copyright (c) 2025 by ${git_name_email}, All Rights Reserved. 
'''
# logger.py
import logging
import logging.handlers
import json
import time
from typing import Optional, Dict,Union
import os
from datetime import datetime
from pathlib import Path
from datetime import datetime
import os
1


class JSONFormatter(logging.Formatter):
    def format(self, record):
        base = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
        }
        # allow extra fields passed via logger.bind(...)
        if hasattr(record, "extra") and isinstance(record.extra, dict):
            base.update(record.extra)
        # include exception info if exists
        if record.exc_info:
            base["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(base, ensure_ascii=False)


def alloc_run_id(base_dir: str = "/home/yc/sdn_qos/logs"):
    """
    在 base_dir 下生成一个形如 YYYYMMDD_N 的实验目录，并返回:
      (run_id, run_dir)

    例如：
      20251127_1
      20251127_2
    """
    os.makedirs(base_dir, exist_ok=True)

    today = datetime.now().strftime("%Y%m%d")
    prefix = today + "_"

    max_idx = 0
    for name in os.listdir(base_dir):
        if not name.startswith(prefix):
            continue
        # name = "YYYYMMDD_N"
        try:
            idx_str = name.split("_", 1)[1]
            idx = int(idx_str)
        except (IndexError, ValueError):
            continue
        max_idx = max(max_idx, idx)

    next_idx = max_idx + 1
    run_id = f"{today}_{next_idx}"
    run_dir = os.path.join(base_dir, run_id)
    os.makedirs(run_dir, exist_ok=True)
    return run_id, run_dir

class ExperimentLogger:
    def __init__(self,
                 base_dir: str = "/home/yc/sdn_qos/logs",
                 run_id:  Optional[str] = None):
        """
        base_dir: 日志根目录
        run_id  : 实验ID，不传的话用当前时间戳生成
        """
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)  # 确保目录存在

        if run_id is None:
             # 获取所有子目录
            subdirs = [d for d in os.listdir(self.base_dir) 
                      if os.path.isdir(self.base_dir / d)]
            
            # 按目录名排序（按时间顺序，最新在最后）
            # 1. 过滤出符合格式的目录（格式：YYYYMMDD-数字）
            # 2. 按日期和序号排序
            valid_dirs = []
            for d in subdirs:
                if '_' in d:
                    date_part = d.split('_')[0]
                    if len(date_part) == 8 and date_part.isdigit():
                        valid_dirs.append(d)
            
            if valid_dirs:
                # 按日期和序号排序（先按日期，再按序号）
                valid_dirs.sort(key=lambda x: (x[:8], int(x.split('_')[1])))
                run_id = valid_dirs[-1]  # 最后一个就是最新
            else:
                # 生成新的run_id（格式：YYYYMMDD-HHMMSS）
                run_id = datetime.now().strftime("%Y%m%d-%H%M%S")

        self.run_id = run_id
        self.run_dir = self.base_dir / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)

    # ---------- FlowProgress 日志 ----------

    def flow_progress_log_path(self, flow_id: Union[int, str]) -> Path:
        """
        /home/yc/sdn_qos/logs/<run_id>/FlowProgress/<flow_id>/progress.log
        """
        flow_dir = self.run_dir / "FlowProgress" / str(flow_id)
        flow_dir.mkdir(parents=True, exist_ok=True)
        return flow_dir / "progress.log"

    # ---------- iperf 日志 ----------

    def _iperf_flow_dir(self, flow_id: Union[int, str],
                        src_ip: str,
                        dst_ip: str) -> Path:
        """
        /home/yc/sdn_qos/logs/<run_id>/iperf/<flow_id>:<src>_to_<dst>/
        """
        dir_name = f"{flow_id}:{src_ip}_to_{dst_ip}"
        flow_dir = self.run_dir / "iperf" / dir_name
        flow_dir.mkdir(parents=True, exist_ok=True)
        return flow_dir

    def iperf_client_log_path(self, flow_id: Union[int, str],
                              src_ip: str,
                              dst_ip: str) -> Path:
        """
        client.log 路径
        """
        flow_dir = self._iperf_flow_dir(flow_id, src_ip, dst_ip)
        return flow_dir / "client.log"

    def iperf_server_log_path(self, flow_id: Union[int, str],
                              src_ip: str,
                              dst_ip: str) -> Path:
        """
        server.log 路径（如果你在 server 端也按流切日志）
        """
        flow_dir = self._iperf_flow_dir(flow_id, src_ip, dst_ip)
        return flow_dir / "server.log"

