'''
Author: yc && qq747339545@163.com
Date: 2025-11-25 09:53:33
LastEditTime: 2025-11-25 10:04:32
FilePath: /sdn_qos/host_agent/host_agent.py
Description: 

Copyright (c) 2025 by ${git_name_email}, All Rights Reserved. 
'''
# host_agent/host_agent.py
#!/usr/bin/env python3
import socket
import json
import subprocess
import sys
import time


def main():
    if len(sys.argv) != 4:
        print("Usage: host_agent.py <controller_ip> <tcp_port> <my_ip>")
        sys.exit(1)

    ctrl_ip = sys.argv[1]
    ctrl_port = int(sys.argv[2])
    my_ip = sys.argv[3]

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((ctrl_ip, ctrl_port))
    f = sock.makefile("rwb")

    # 注册
    reg_line = f"REGISTER {my_ip}\n".encode()
    f.write(reg_line)
    f.flush()
    print(f"[agent] REGISTER sent: {reg_line!r}")

    # 循环等待 permit 消息
    try:
        while True:
            line = f.readline()
            if not line:
                print("[agent] connection closed by controller")
                break
            line = line.decode().strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception as e:
                print("[agent] invalid JSON:", e, line)
                continue

            if msg.get("type") == "permit":
                handle_permit(msg)
    finally:
        sock.close()


def handle_permit(msg: dict):
    flow_id = msg.get("flow_id")
    src_ip = msg.get("src_ip")
    dst_ip = msg.get("dst_ip")
    rate_bps = int(msg.get("send_rate_bps", 0))
    size_bytes = int(msg.get("size_bytes", 0))
    tos = int(msg.get("tos", 0))

    if rate_bps <= 0 or size_bytes <= 0 or not dst_ip:
        print("[agent] invalid permit:", msg)
        return

    # 估算持续时间（秒），留一点余量
    duration = int(size_bytes * 8 / rate_bps) + 1
    rate_str = f"{int(rate_bps/1_000_000)}M" if rate_bps >= 1_000_000 else f"{rate_bps}B"

    cmd = [
        "iperf3",
        "-u",
        "-c", dst_ip,
        "-b", rate_str,
        "-t", str(duration),
        "--tos", str(tos),
    ]
    print(f"[agent] START flow_id={flow_id} cmd:", " ".join(cmd))
    # 后台起一个进程，不等待
    subprocess.Popen(cmd)


if __name__ == "__main__":
    main()
