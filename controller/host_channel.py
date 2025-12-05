# controller/host_channel.py
import socket
import threading
import json
from typing import Dict, Tuple , Optional
import random
import logging
import os 
import time



LOG = logging.getLogger('host_channel')
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

    def __init__(self, host: str, port: int,run_ts: str,port_mgr=None):
        self.host = host
        self.port = port
        self.run_ts = run_ts 
        self.port_mgr = port_mgr
        
        # key: host_ip, value: (permit_port, recv_port)
        self._hosts: Dict[str, Tuple[int, int]] = {}
        self._lock = threading.Lock()
        
        # key: src_ip, value: (host_ip, listen_port)
        # self.host_addrs: Dict[str, Tuple[str, int]] = {}
        # self._server_sock: socket.socket | None = None
        # self._thread: threading.Thread | None = None

    
    def _append_flow_progress(self, flow_id: int, line: str):
        """
        直接往 FlowProgress/<flow_id>/progress.log 追加一行，带时间戳。
        依赖 run_ts + 固定 base_dir=/home/yc/sdn_qos/logs。
        """
        if not self.run_ts:
            return

        base_dir = "/home/yc/sdn_qos/logs"
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        flow_dir = os.path.join(base_dir, self.run_ts, "FlowProgress", str(flow_id))
        os.makedirs(flow_dir, exist_ok=True)
        log_path = os.path.join(flow_dir, "progress.log")
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"{ts} {line}\n")
        except Exception as e:
            LOG.warning("[HostChannel] _append_flow_progress failed: %s", e)


    # ---------- 注册部分：由 REST 调用 ----------

    def register_host(self, host_ip: str, permit_port: int, recv_port: int):
        with self._lock:
            # 希望recv_port随机分配
            
            self._hosts[host_ip] = (permit_port, recv_port)
        LOG.info(
            "[HostChannel] register_host: ip=%s permit_port=%d recv_port=%d, all_hosts=%s",
            host_ip, permit_port, recv_port, self._hosts
        )

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
            LOG.info("[HostChannel] pick_dst_for_flow src_ip=%s candidates=%s",
                 src_ip, candidates)
        if not candidates:
            return None
        dst = random.choice(candidates)
        LOG.info("[HostChannel] picked dst for src=%s -> %s", src_ip, dst)
        return dst

    # ---------- PERMIT 推送部分：由 GlobalScheduler 调用 ----------
    def send_flow_prepare(self, flow):
        """
        主动连到 dst_ip 所在的 host 的 permit_port，发送 FLOW_PREPARE 消息。
        告诉对端：
        - 这条流的 flow_id
        - src_ip / src_port
        - dst_ip / dst_port
        - 速率 / 大小 / dscp 等（可选，用来做检查或日志）
        """
        dst_ip = flow.dst_ip
        with self._lock:
            info = self._hosts.get(dst_ip)

        if not info:
            LOG.warning("[HostChannel] no host info for dst_ip=%s, "
                        "skip FLOW_PREPARE for flow_id=%s", dst_ip, flow.id)
            return
        
        permit_port, recv_port = info


        msg = {
            "type": "FLOW_PREPARE",
            "flow_id": flow.id,
            "src_ip": flow.src_ip,
            "dst_ip": flow.dst_ip,
            "src_port": flow.src_port,
            "dst_port": flow.dst_port,
            "send_rate_bps": flow.send_rate_bps,
            "size_bytes": flow.size_bytes,
            "dscp": flow.dscp,
        }
        if self.run_ts is not None:
            msg["run_ts"] = self.run_ts

        data = (json.dumps(msg) + "\n").encode("utf-8")
        LOG.info(
            "[HostChannel] send_flow_prepare: flow_id=%s %s:%s -> %s:%s",
            flow.id, flow.src_ip, flow.src_port, flow.dst_ip,flow.dst_port
        )
        try:
            with socket.create_connection((dst_ip, permit_port), timeout=3.0) as s:
                s.sendall(data)
        except OSError as e:
            LOG.warning("[HostChannel] failed to send FLOW_PREPARE to %s:%s: %s",
                        dst_ip, permit_port, e)
            
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
            LOG.warning("[HostChannel] no host info for src_ip=%s, "
                        "skip PERMIT for flow_id=%s", src_ip, flow.id)
            return

        permit_port, _recv_port = info
        dst_port = getattr(flow, "dst_port", None)
        src_port = getattr(flow, "src_port", None)   # 新增

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
        if src_port is not None:
            msg["src_port"] = src_port    
            
        if self.run_ts is not None:
            msg["run_ts"] = self.run_ts

        data = (json.dumps(msg) + "\n").encode("utf-8")
        LOG.info(
            "[HostChannel] send_permit: flow_id=%s src=%s dst=%s:%s "
            "rate=%s size=%s dscp=%s",
            flow.id, flow.src_ip, flow.dst_ip, dst_port,
            flow.send_rate_bps, flow.size_bytes, getattr(flow, "dscp", 0),
        )
        try:
            with socket.create_connection((src_ip, permit_port), timeout=3.0) as s:
                s.sendall(data)

            log_line = (f"[HostChannel] sent PERMIT to {src_ip}:{permit_port} "
                        f"for flow_id={flow.id}")
            print(log_line)
            # ➕ 写入对应 flow 的 FlowProgress
            self._append_flow_progress(flow.id, log_line)

        except OSError as e:
            err_line = (f"[HostChannel] failed to send PERMIT to {src_ip}:{permit_port}: {e}")
            print(err_line)
            # 失败也可以记录一下（可选）
            self._append_flow_progress(flow.id, err_line)






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
