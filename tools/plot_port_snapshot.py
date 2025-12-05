'''
Author: yc && qq747339545@163.com
Date: 2025-12-01 15:16:36
LastEditTime: 2025-12-01 15:16:42
FilePath: /sdn_qos/tools/plot_port_snapshot.py
Description: 

Copyright (c) 2025 by ${git_name_email}, All Rights Reserved. 
'''
#!/usr/bin/env python3
"""
从 PortSnapshot/port_snapshot.log 解析端口带宽随时间的变化并画图。

用法示例：
    python plot_port_snapshot.py \
        /home/yc/sdn_qos/logs/20251128_1/PortSnapshot/port_snapshot.log \
        --dpid 1 --port 2
"""

import argparse
from datetime import datetime
import re

import matplotlib.pyplot as plt


SNAPSHOT_TS_FMT = "%Y-%m-%d %H:%M:%S"


def parse_log(log_path: str, target_dpid: int, target_port: int):
    """
    解析日志，返回 time_list, reserved_list, avail_list
    """
    time_list = []
    reserved_list = []
    avail_list = []

    cur_ts = None

    header_re = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[PortSnapshot\]")
    line_re = re.compile(
        r"^\s*s(\d+):(\d+)\s+cap=(\d+)\s+reserved=(\d+)\s+avail=(\d+)\s+"
        r"gold=(\d+)\s+silver=(\d+)\s+best=(\d+)"
    )

    with open(log_path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")

            # 1) header: 时间戳 + [PortSnapshot]
            m = header_re.match(line)
            if m:
                ts_str = m.group(1)
                cur_ts = datetime.strptime(ts_str, SNAPSHOT_TS_FMT)
                continue

            # 2) 端口行：  s1:2 cap=... reserved=... ...
            m = line_re.match(line)
            if not m or cur_ts is None:
                continue

            dpid = int(m.group(1))
            port = int(m.group(2))
            # cap = int(m.group(3))
            reserved = int(m.group(4))
            avail = int(m.group(5))

            if dpid == target_dpid and port == target_port:
                time_list.append(cur_ts)
                reserved_list.append(reserved)
                avail_list.append(avail)

    return time_list, reserved_list, avail_list


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("log_path", help="port_snapshot.log 路径")
    parser.add_argument("--dpid", type=int, required=True, help="交换机 DPID，例如 1")
    parser.add_argument("--port", type=int, required=True, help="端口号，例如 2")
    args = parser.parse_args()

    t, reserved, avail = parse_log(args.log_path, args.dpid, args.port)
    if not t:
        print("没有解析到任何数据，确认一下 dpid/port 和日志路径是否正确。")
        return

    # bps -> Mbps
    reserved_m = [x / 1e6 for x in reserved]
    avail_m = [x / 1e6 for x in avail]

    plt.figure()
    plt.plot(t, reserved_m, label="reserved (Mbps)")
    plt.plot(t, avail_m, label="available (Mbps)")

    plt.xlabel("time")
    plt.ylabel("Mbps")
    plt.title(f"Port s{args.dpid}:{args.port} bandwidth over time")
    plt.legend()
    plt.grid(True)
    plt.tight_layout()
    plt.show()


if __name__ == "__main__":
    main()
