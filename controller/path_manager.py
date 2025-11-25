'''
Author: yc && qq747339545@163.com
Date: 2025-11-25 09:50:49
LastEditTime: 2025-11-25 16:55:28
FilePath: /sdn_qos/controller/path_manager.py
Description: Path 管理模块

Copyright (c) 2025 by ${git_name_email}, All Rights Reserved. 
'''
# controller/path_manager.py
import os
import yaml
from typing import Dict, List, Tuple


class PathManager:
    """
    简化版 PathManager：
    - 从 topo_config.yml 中读取静态路径:
      paths:
        "10.0.1.1-10.0.3.1":
          - [1, 2]
          - [2, 3]
          - [3, 1]
    - 返回 [(dpid, out_port), ...]
    """

    def __init__(self, config_path: str):
        self.config_path = config_path
        self.paths: Dict[str, List[Tuple[int, int]]] = {}
        self._load_config()

    def _load_config(self):
        if not os.path.exists(self.config_path):
            return
        with open(self.config_path, "r") as f:
            data = yaml.safe_load(f) or {}
        paths_cfg = data.get("paths", {})
        for key, hop_list in paths_cfg.items():
            # hop_list: [[dpid, port], ...]
            self.paths[key] = [(int(dpid), int(port)) for dpid, port in hop_list]

    def get_path(self, src_ip: str, dst_ip: str) -> List[Tuple[int, int]]:
        key = f"{src_ip}-{dst_ip}"
        if key in self.paths:
            return self.paths[key][:]
        # 反向也尝试一下
        rev_key = f"{dst_ip}-{src_ip}"
        if rev_key in self.paths:
    # 翻转路径：[1,2] -> [2,1]，[2,3] -> [3,2]...
            reversed_path = [(dpid, port) for dpid, port in reversed(self.paths[rev_key])]
            return reversed_path
        # 找不到时返回空列表
        return []
