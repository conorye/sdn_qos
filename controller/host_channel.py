'''
Author: yc && qq747339545@163.com
Date: 2025-11-25 09:51:34
LastEditTime: 2025-11-25 10:03:22
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
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self):
        while True:
            conn, addr = self._server_sock.accept()
            t = threading.Thread(target=self._handle_client, args=(conn,), daemon=True)
            t.start()

    def _handle_client(self, conn: socket.socket):
        src_ip = None
        try:
            f = conn.makefile("rwb")
            line = f.readline().decode().strip()
            if line.startswith("REGISTER "):
                src_ip = line.split(" ", 1)[1]
                with self._lock:
                    self.host_sockets[src_ip] = conn
            else:
                conn.close()
                return

            # 之后只是保持连接，真正发送在 send_permit 里
            while True:
                # 不需要读东西，保持阻塞即可
                data = f.readline()
                if not data:
                    break
        except Exception:
            pass
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
