#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从 FlowProgress/<flow_id>/progress.log 和
     Flow_PortState/<flow_id>.log
解析单条流的带宽随时间变化，并画图。

功能：
1. 读取 progress.log，提取：
   - sent(last_hop) [MB]
   - rate(last_hop) [Mbps]
   - status

2. 读取 Flow_PortState/<flow_id>.log，提取：
   - ReservePath / ReleasePath / PortReserve / PortReleaseSingle / PortRelease 时间点

3. 从 progress.log 中提取 [TailRelease] 相关时间点。

4. 画两张图：
   - 图1：rate(Mbps) vs time，标出各种事件的竖线
   - 图2：sent(MB) vs time，同样标出事件
"""

import argparse
import os
import re
from datetime import datetime

import matplotlib.pyplot as plt


# FlowProgress 日志的时间戳格式：2025-11-28 21:33:41 ...
TS_FMT = "%Y-%m-%d %H:%M:%S"


def parse_flow_progress(progress_log_path: str, flow_id: int):
    """
    解析 FlowProgress/<flow_id>/progress.log

    返回：
        times:      [datetime, ...]
        sent_mb:    [float, ...]
        rate_mbps:  [float, ...]
        status:     [str, ...]
        tail_events: [datetime, ...]  # [TailRelease] 出现的时间
    """
    times = []
    sent_mb = []
    rate_mbps = []
    status_list = []
    tail_events = []

    # 正则：FlowProgress 头
    header_re = re.compile(
        r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[FlowProgress\] flow=(\d+)"
    )

    # sent(last_hop)=XX.XXMB / YY.YYMB
    sent_re = re.compile(
        r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s+sent\(last_hop\)=([\d\.]+)MB / ([\d\.]+)MB"
    )

    # rate(last_hop)=XX.XXMbps eta=XX.Xs
    rate_re = re.compile(
        r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s+rate\(last_hop\)=([\d\.]+)Mbps"
    )

    # hop_bytes ... status=allowed/finished
    status_re = re.compile(r".*status=([a-zA-Z_]+)")

    # TailRelease
    tail_re = re.compile(
        r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[TailRelease\] flow=(\d+)"
    )

    cur_ts = None
    cur_sent = None
    cur_rate = None
    cur_status = None

    with open(progress_log_path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")

            # 1) TailRelease 单独记录
            m_tail = tail_re.match(line)
            if m_tail:
                ts_str, fid_str = m_tail.groups()
                fid = int(fid_str)
                if fid == flow_id:
                    tail_events.append(datetime.strptime(ts_str, TS_FMT))
                # TailRelease 行不参与 sent/rate 提取，继续下一行
                continue

            # 2) FlowProgress 头：更新当前时间戳 & flow_id 检查
            m_header = header_re.match(line)
            if m_header:
                ts_str, fid_str = m_header.groups()
                fid = int(fid_str)
                if fid != flow_id:
                    # 其他 flow 的日志，直接跳过
                    cur_ts = None
                    continue
                cur_ts = datetime.strptime(ts_str, TS_FMT)
                cur_sent = None
                cur_rate = None
                cur_status = None
                continue

            if cur_ts is None:
                # 当前不在指定 flow 的 block 里
                continue

            # 3) sent 行
            m_sent = sent_re.match(line)
            if m_sent:
                sent_val_str, _total_str = m_sent.groups()
                try:
                    cur_sent = float(sent_val_str)
                except ValueError:
                    cur_sent = None
                continue

            # 4) rate 行
            m_rate = rate_re.match(line)
            if m_rate:
                rate_val_str = m_rate.group(1)
                try:
                    cur_rate = float(rate_val_str)
                except ValueError:
                    cur_rate = None
                # 注意：不要在这里 return，后面还有 status 行
                # 我们选择在看到 status 行之后才 append 一次数据点
                continue

            # 5) hop_bytes + status 行
            m_status = status_re.match(line)
            if m_status:
                cur_status = m_status.group(1)

                # 到了 block 的最后一行（包含 status），再统一 append：
                if (cur_sent is not None) and (cur_rate is not None):
                    times.append(cur_ts)
                    sent_mb.append(cur_sent)
                    rate_mbps.append(cur_rate)
                    status_list.append(cur_status)

                # 当前 block 结束，等待下一个 FlowProgress
                cur_ts = None
                cur_sent = None
                cur_rate = None
                cur_status = None
                continue

    return times, sent_mb, rate_mbps, status_list, tail_events


def parse_flow_portstate(flow_portstate_log_path: str, flow_id: int):
    """
    解析 Flow_PortState/<flow_id>.log

    返回一个 dict:
        {
          "ReservePath":   [datetime, ...],
          "ReleasePath":   [datetime, ...],
          "PortReserve":   [datetime, ...],
          "PortRelease":   [datetime, ...],
          "PortReleaseSingle": [datetime, ...],
        }
    """
    events = {
        "ReservePath": [],
        "ReleasePath": [],
        "PortReserve": [],
        "PortRelease": [],
        "PortReleaseSingle": [],
    }

    if not os.path.isfile(flow_portstate_log_path):
        # 没开 Flow_PortState 功能也没关系，仅返回空事件
        return events

    # 通用：时间戳在最前面
    ts_header_re = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[(\w+)\]")

    with open(flow_portstate_log_path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            m = ts_header_re.match(line)
            if not m:
                continue

            ts_str, tag = m.groups()
            if tag not in events:
                continue

            ts = datetime.strptime(ts_str, TS_FMT)
            events[tag].append(ts)

    return events


def plot_flow(times, sent_mb, rate_mbps,
              status_list,
              tail_events,
              port_events,
              flow_id: int):
    """
    画两张图：
      1) rate vs time
      2) sent vs time

    并标记：
      - ReservePath / ReleasePath
      - PortReleaseSingle（TailRelease 每一跳）
      - PortRelease
      - TailRelease（StatsCollector 那一条）
    """

    # ---------- 图1：rate ----------
    plt.figure()
    plt.plot(times, rate_mbps, label="rate(last_hop) [Mbps]")
    plt.xlabel("time")
    plt.ylabel("Mbps")
    plt.title(f"Flow {flow_id} rate(last_hop) over time")
    plt.grid(True)

    # 标记各种事件
    # ReservePath / ReleasePath
    for ts in port_events.get("ReservePath", []):
        plt.axvline(ts, linestyle="--", linewidth=1.0, label="ReservePath")
        # 只标一次 legend
        break
    for ts in port_events.get("ReleasePath", []):
        plt.axvline(ts, linestyle="--", linewidth=1.0, label="ReleasePath")
        break

    # PortReleaseSingle
    if port_events.get("PortReleaseSingle"):
        for ts in port_events["PortReleaseSingle"]:
            plt.axvline(ts, linestyle=":", linewidth=0.8)
        # legend 只要一个 label
        plt.axvline(
            port_events["PortReleaseSingle"][0],
            linestyle=":", linewidth=0.8, label="PortReleaseSingle"
        )

    # PortRelease
    if port_events.get("PortRelease"):
        for ts in port_events["PortRelease"]:
            plt.axvline(ts, linestyle="-.", linewidth=0.8)
        plt.axvline(
            port_events["PortRelease"][0],
            linestyle="-.", linewidth=0.8, label="PortRelease"
        )

    # TailRelease（StatsCollector 打的）
    if tail_events:
        for ts in tail_events:
            plt.axvline(ts, linewidth=1.2)
        plt.axvline(
            tail_events[0],
            linewidth=1.2,
            label="TailRelease(log)",
        )

    plt.legend()
    plt.tight_layout()

    # ---------- 图2：sent ----------
    plt.figure()
    plt.plot(times, sent_mb, label="sent(last_hop) [MB]")
    plt.xlabel("time")
    plt.ylabel("MB")
    plt.title(f"Flow {flow_id} sent(last_hop) over time")
    plt.grid(True)

    # 同样标记事件
    for ts in port_events.get("ReservePath", []):
        plt.axvline(ts, linestyle="--", linewidth=1.0, label="ReservePath")
        break
    for ts in port_events.get("ReleasePath", []):
        plt.axvline(ts, linestyle="--", linewidth=1.0, label="ReleasePath")
        break

    if port_events.get("PortReleaseSingle"):
        for ts in port_events["PortReleaseSingle"]:
            plt.axvline(ts, linestyle=":", linewidth=0.8)
        plt.axvline(
            port_events["PortReleaseSingle"][0],
            linestyle=":", linewidth=0.8, label="PortReleaseSingle"
        )

    if port_events.get("PortRelease"):
        for ts in port_events["PortRelease"]:
            plt.axvline(ts, linestyle="-.", linewidth=0.8)
        plt.axvline(
            port_events["PortRelease"][0],
            linestyle="-.", linewidth=0.8, label="PortRelease"
        )

    if tail_events:
        for ts in tail_events:
            plt.axvline(ts, linewidth=1.2)
        plt.axvline(
            tail_events[0],
            linewidth=1.2,
            label="TailRelease(log)",
        )

    plt.legend()
    plt.tight_layout()

    plt.show()


def main():
    parser = argparse.ArgumentParser(
        description="画单条 Flow 的速率/已发送字节随时间变化，并标记尾部释放等事件"
    )
    parser.add_argument(
        "run_dir",
        help="本次实验的 run 目录，例如 /home/yc/sdn_qos/logs/20251128_1",
    )
    parser.add_argument(
        "--flow-id", "-f", type=int, required=True,
        help="要分析的 flow_id，例如 20000",
    )
    args = parser.parse_args()

    run_dir = args.run_dir
    flow_id = args.flow_id

    # 构造日志路径
    progress_log_path = os.path.join(
        run_dir, "FlowProgress", str(flow_id), "progress.log"
    )
    portstate_log_path = os.path.join(
        run_dir, "Flow_PortState", f"{flow_id}.log"
    )

    if not os.path.isfile(progress_log_path):
        print(f"找不到 progress.log: {progress_log_path}")
        return

    print(f"[INFO] 使用 FlowProgress 日志: {progress_log_path}")
    if os.path.isfile(portstate_log_path):
        print(f"[INFO] 使用 Flow_PortState 日志: {portstate_log_path}")
    else:
        print(f"[WARN] 没找到 Flow_PortState 日志: {portstate_log_path}")

    # 解析 FlowProgress
    (times,
     sent_mb,
     rate_mbps,
     status_list,
     tail_events) = parse_flow_progress(progress_log_path, flow_id)

    if not times:
        print("没有从 progress.log 解析到任何数据，确认 flow_id 是否正确。")
        return

    # 解析 Flow_PortState
    port_events = parse_flow_portstate(portstate_log_path, flow_id)

    # 画图
    plot_flow(times, sent_mb, rate_mbps,
              status_list, tail_events, port_events, flow_id)


if __name__ == "__main__":
    main()
