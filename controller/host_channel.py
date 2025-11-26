# controller/host_channel.py
import socket
import threading
import json
from typing import Dict, Tuple , Optional
import random

class HostChannel:
    """
    Host 通信模块（REST + 主动 TCP）：

    - register_host(host_ip, permit_port, recv_port)
      由 REST /scheduler/register_host 调用，把 Host 的信息记录下来。

    - pick_dst_for_flow(src_ip)
      调度器在创建 Flow 时，调用它随机挑一个目的 Host（返回 dst_ip, dst_recv_port）。

    - send_permit(flow)
      调度器 admission 通过后，调用它主动连到 src_ip 对应的 Host 的 permit_port，
      发送一条 PERMIT 消息（JSON），告诉 Host：
        - 这条流的 flow_id
        - 你要往哪个 dst_ip:dst_port 发
        - 发送速率 send_rate_bps、总大小 size_bytes、dscp 等
    """

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
       

        
        # key: host_ip, value: (permit_port, recv_port)
        self._hosts: Dict[str, Tuple[int, int]] = {}
        self._lock = threading.Lock()
        
        # key: src_ip, value: (host_ip, listen_port)
        # self.host_addrs: Dict[str, Tuple[str, int]] = {}
        # self._server_sock: socket.socket | None = None
        # self._thread: threading.Thread | None = None


    # ---------- 注册部分：由 REST 调用 ----------

    def register_host(self, host_ip: str, permit_port: int, recv_port: int):
        with self._lock:
            self._hosts[host_ip] = (permit_port, recv_port)
        print(f"[HostChannel] host registered: ip={host_ip}, "
              f"permit_port={permit_port}, recv_port={recv_port}")

    def pick_dst_for_flow(self, src_ip: str) -> Optional[Tuple[str, int]]:
        """
        为某个 src_ip 挑一个目的 host（目前简单随机，排除掉自己）
        返回: (dst_ip, dst_recv_port)，如果没有可用目的 host 则返回 None
        """
        with self._lock:
            candidates = [
                (ip, info[1])   # info[1] = recv_port
                for ip, info in self._hosts.items()
                if ip != src_ip
            ]
        if not candidates:
            return None
        return random.choice(candidates)

    # ---------- PERMIT 推送部分：由 GlobalScheduler 调用 ----------

    def send_permit(self, flow):
        """
        主动连到 src_ip 所在的 host 的 permit_port，发送 PERMIT 消息。
        要求 flow 至少有：
          - id, src_ip, dst_ip, send_rate_bps, size_bytes, dscp
          - (可选) dst_port: 目的端口，便于 host 用 iperf3 -p
        """
        src_ip = flow.src_ip
        with self._lock:
            info = self._hosts.get(src_ip)

        if not info:
            print(f"[HostChannel] no host info for src_ip={src_ip}, "
                  f"skip PERMIT for flow_id={flow.id}")
            return

        permit_port, _recv_port = info
        dst_port = getattr(flow, "dst_port", None)

        msg = {
            "type": "PERMIT",
            "flow_id": flow.id,
            "src_ip": flow.src_ip,
            "dst_ip": flow.dst_ip,
            "send_rate_bps": flow.send_rate_bps,
            "size_bytes": flow.size_bytes,
            "dscp": flow.dscp,
        }
        if dst_port is not None:
            msg["dst_port"] = dst_port

        data = (json.dumps(msg) + "\n").encode("utf-8")

        try:
            with socket.create_connection((src_ip, permit_port), timeout=3.0) as s:
                s.sendall(data)
            print(f"[HostChannel] sent PERMIT to {src_ip}:{permit_port} "
                  f"for flow_id={flow.id}")
        except OSError as e:
            print(f"[HostChannel] failed to send PERMIT to {src_ip}:{permit_port}: {e}")





    # ------------ 注册 Server 部分 ------------

    # def start(self):
    #     """启动 REGISTER TCP Server"""
    #     self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    #     self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    #     self._server_sock.bind((self.host, self.port))
    #     self._server_sock.listen(5)
    #     print(f"[HostChannel] REGISTER server listening on {self.host}:{self.port}")
    #     self._thread = threading.Thread(target=self._accept_loop, daemon=True)
    #     self._thread.start()

    # def _accept_loop(self):
    #     assert self._server_sock is not None
    #     while True:
    #         try:
    #             conn, addr = self._server_sock.accept()
    #         except OSError:
    #             break
    #         t = threading.Thread(
    #             target=self._handle_register,
    #             args=(conn, addr),
    #             daemon=True
    #         )
    #         t.start()

    # def _handle_register(self, conn: socket.socket, addr):
    #     """
    #     处理一次 REGISTER：
    #     - 读一行 JSON
    #     - 解析 type=REGISTER, src_ip, listen_port
    #     - 存到 self.host_addrs
    #     - 打印日志然后关连接
    #     """
    #     src_ip = None
    #     try:
    #         f = conn.makefile("rwb")
    #         line_bytes = f.readline()
    #         if not line_bytes:
    #             print(f"[HostChannel] empty REGISTER from {addr}")
    #             return

    #         line = line_bytes.decode().strip()
    #         try:
    #             msg = json.loads(line)
    #         except json.JSONDecodeError as e:
    #             print(f"[HostChannel] invalid JSON from {addr}: {e}, raw={line!r}")
    #             return

    #         msg_type = str(msg.get("type", "")).upper()
    #         if msg_type != "REGISTER":
    #             print(f"[HostChannel] unexpected msg type from {addr}: {msg}")
    #             return

    #         src_ip = msg.get("src_ip")
    #         listen_port = int(msg.get("listen_port", 0))
    #         # 可选：host_ip 也可以从消息里带；没有就用 src_ip
    #         host_ip = msg.get("host_ip") or src_ip

    #         if not src_ip or listen_port <= 0:
    #             print(f"[HostChannel] invalid REGISTER from {addr}: {msg}")
    #             return

    #         with self._lock:
    #             self.host_addrs[src_ip] = (host_ip, listen_port)

    #         # ✅ 你想要的“每次注册打印出来”就在这里：
    #         print(
    #             f"[HostChannel] REGISTER OK: src_ip={src_ip}, "
    #             f"listen={host_ip}:{listen_port}, from={addr}"
    #         )

    #     except Exception as e:
    #         print(f"[HostChannel] _handle_register error from {addr}: {e}")
    #     finally:
    #         conn.close()

    # # ------------ PERMIT 推送（Client）部分 ------------

    # def send_permit(self, flow):
    #     """
    #     向对应 src_ip 的 host 主动建立 TCP 连接并发送 PERMIT 消息。
    #     flow 必须有：id, src_ip, dst_ip, send_rate_bps, size_bytes, dscp
    #     """
    #     with self._lock:
    #         info = self.host_addrs.get(flow.src_ip)

    #     if not info:
    #         print(f"[HostChannel] no registered host for src_ip={flow.src_ip}, skip PERMIT")
    #         return

    #     host_ip, listen_port = info
    #     msg = {
    #         "type": "PERMIT",              # 注意这里大写，方便 host 端统一判断
    #         "flow_id": flow.id,
    #         "src_ip": flow.src_ip,
    #         "dst_ip": flow.dst_ip,
    #         "send_rate_bps": flow.send_rate_bps,
    #         "size_bytes": flow.size_bytes,
    #         "dscp": flow.dscp,            # 明确用 dscp 字段
    #     }
    #     data = (json.dumps(msg) + "\n").encode("utf-8")

    #     try:
    #         with socket.create_connection((host_ip, listen_port), timeout=3.0) as s:
    #             s.sendall(data)
    #         print(
    #             f"[HostChannel] sent PERMIT to {host_ip}:{listen_port} "
    #             f"for flow_id={flow.id}"
    #         )
    #     except OSError as e:
    #         print(f"[HostChannel] failed to send PERMIT to {host_ip}:{listen_port}: {e}")
