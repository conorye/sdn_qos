'''
Author: yc && qq747339545@163.com
Date: 2025-11-25 09:51:34
LastEditTime: 2025-11-26 15:06:23
FilePath: /sdn_qos/controller/host_channel.py
Description: Host 通信模块

Copyright (c) 2025 by ${git_name_email}, All Rights Reserved. 
'''
# controller/host_channel.py
import socket
import threading
import json
from typing import Dict


class HostChannel:
    """
    控制器侧 TCP Server：
    - 接收 host REGISTER
    - 向 host 推送 permit 消息
    """ 

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.host_sockets: Dict[str, socket.socket] = {}  # src_ip -> socket
        self._server_sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self):
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.bind((self.host, self.port))
        self._server_sock.listen(5)
        print(f"✅ TCP Server started on {self.host}:{self.port}")  # 添加这行
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self):
        while True:
            conn, addr = self._server_sock.accept()
            t = threading.Thread(target=self._handle_client, args=(conn,), daemon=True)
            t.start()

    def _handle_client(self, conn: socket.socket):
        src_ip = None
        addr = conn.getpeername()
        try:
            f = conn.makefile("rwb")
            while True:
                line_bytes = f.readline()
                if not line_bytes:
                    print(f"[HostChannel] connection closed from {addr}")
                    break

                line = line_bytes.decode().strip()
                if not line:
                    continue

                # 这里假设 host_agent 发送的是 JSON 格式：
                # {"type": "REGISTER", "src_ip": "10.0.0.1"}
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError as e:
                    print(f"[HostChannel] invalid JSON from {addr}: {e}, raw={line!r}")
                    continue

                mtype = str(msg.get("type", "")).upper()

                if mtype == "REGISTER":
                    src_ip = msg.get("src_ip")
                    # 你要的打印信息在这里：
                    print(f"[HostChannel] REGISTER from {src_ip}: {msg}")
                    with self._lock:
                        self.host_sockets[src_ip] = conn

                # 下面预留给“流注册”之类的扩展（后面会讲）
                elif mtype == "FLOW_REQUEST":
                    print(f"[HostChannel] FLOW_REQUEST from {msg.get('src_ip')}: {msg}")
                    # TODO: 在这里调用调度器 new_flow(...)
                else:
                    print(f"[HostChannel] unknown message type {mtype}: {msg}")

        except Exception as e:
            print(f"[HostChannel] _handle_client error from {addr}: {e}")
        finally:
            if src_ip:
                with self._lock:
                    self.host_sockets.pop(src_ip, None)
            conn.close()


    def send_permit(self, flow):
        """
        向对应 src_ip 的 host 发送 permit 消息。
        flow 必须有：id, src_ip, dst_ip, send_rate_bps, size_bytes, dscp
        """
        with self._lock:
            conn = self.host_sockets.get(flow.src_ip)
        if not conn:
            return
        msg = {
            "type": "permit",
            "flow_id": flow.id,
            "src_ip": flow.src_ip,
            "dst_ip": flow.dst_ip,
            "send_rate_bps": flow.send_rate_bps,
            "size_bytes": flow.size_bytes,
            "tos": flow.dscp,
        }
        try:
            data = (json.dumps(msg) + "\n").encode()
            conn.sendall(data)
        except Exception:
            # 发送失败就忽略，实际可加日志
            pass
