# host_agent/host_agent.py
#!/usr/bin/env python3
import socket
import json
import subprocess
import sys
import time
import threading
import logging
import urllib.request


logger = logging.getLogger("host_agent")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)


# # --------- 与 RYU 的注册 ----------

# def register_to_controller(ctrl_ip: str, register_port: int,
#                            my_ip: str, listen_port: int):
#     """
#     向 RYU 的 HostChannel 注册：
#     - TCP client 连接 ctrl_ip:register_port
#     - 发送一行 JSON: {"type":"REGISTER","src_ip":my_ip,"listen_port":listen_port}
#     - 然后断开
#     """
#     msg = {
#         "type": "REGISTER",
#         "src_ip": my_ip,
#         "listen_port": listen_port,
#     }
#     data = (json.dumps(msg) + "\n").encode("utf-8")

#     logger.info(
#         f"Registering to controller {ctrl_ip}:{register_port} "
#         f"with src_ip={my_ip}, listen_port={listen_port}"
#     )

#     sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
#     sock.settimeout(5.0)
#     try:
#         sock.connect((ctrl_ip, register_port))
#         sock.sendall(data)
#         logger.info("REGISTER sent successfully")
#     except OSError as e:
#         logger.error(f"Failed to register to controller: {e}")
#     finally:
#         sock.close()


# --------- PERMIT 推送接收 Server ----------

def handle_permit(msg: dict):
    """
    收到 PERMIT 后，启动 iperf3 发送流（你原来的逻辑可以搬过来）
    """
    flow_id = msg.get("flow_id")
    src_ip = msg.get("src_ip")
    dst_ip = msg.get("dst_ip")
    rate_bps = int(msg.get("send_rate_bps", 0))
    size_bytes = int(msg.get("size_bytes", 0))
    dscp = int(msg.get("dscp", 0))

    if rate_bps <= 0 or size_bytes <= 0 or not dst_ip:
        logger.error(f"Invalid PERMIT: {msg}")
        return

    # DSCP → TOS
    tos = dscp << 2
    duration = int(size_bytes * 8 / rate_bps) + 1
    if rate_bps >= 1_000_000:
        rate_str = f"{int(rate_bps / 1_000_000)}M"
    else:
        rate_str = f"{rate_bps}B"

    cmd = [
        "iperf3",
        "-u",
        "-c", dst_ip,
        "-b", rate_str,
        "-t", str(duration),
        "--tos", str(tos),
    ]
    logger.info(f"START flow_id={flow_id} cmd: {' '.join(cmd)}")
    subprocess.Popen(cmd)


def start_permit_server(listen_ip: str, listen_port: int):
    """
    起一个 TCP Server，监听 listen_ip:listen_port
    RYU 每次推 PERMIT 都会主动连过来，发一行 JSON 后断开
    """

    def _handle_conn(conn: socket.socket, addr):
        try:
            f = conn.makefile("rb")
            for line_bytes in f:
                line = line_bytes.decode("utf-8").strip()
                if not line:
                    continue
                logger.info(f"Received from controller {addr}: {line}")
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError as e:
                    logger.error(f"Invalid JSON from controller: {e}, raw={line!r}")
                    continue

                msg_type = str(msg.get("type", "")).upper()
                if msg_type == "PERMIT":
                    handle_permit(msg)
                else:
                    logger.warning(f"Unknown message type from controller: {msg}")
        finally:
            conn.close()

    def _server_loop():
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((listen_ip, listen_port))
        server.listen(5)
        logger.info(f"PERMIT server listening on {listen_ip}:{listen_port}")

        while True:
            conn, addr = server.accept()
            t = threading.Thread(target=_handle_conn, args=(conn, addr), daemon=True)
            t.start()

    t = threading.Thread(target=_server_loop, daemon=True)
    t.start()
    return t

def report_flow_finished(controller_ip: str, flow_id: int,
                         bytes_received: int, rest_port: int = 8080):
    """
    调用 RYU REST 告诉调度器：某个 flow 已完成。
    需要在 scheduler_app 里增加 /scheduler/host_report，下面第 3 节会讲。
    """
    url = f"http://{controller_ip}:{rest_port}/scheduler/host_report"
    payload = {
        "flow_id": flow_id,
        "status": "finished",
        "bytes_received": bytes_received,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            body = resp.read().decode("utf-8")
            logger.info(f"report_flow_finished resp: {resp.status} {body}")
    except Exception as e:
        logger.error(f"report_flow_finished error: {e}")


def start_flow_receiver(listen_ip: str, data_port: int,
                        controller_ip: str, rest_port: int = 8080):
    """
    简化版“流接收器”骨架：
    - 接受一个连接
    - 第一行 JSON 说明 flow_id / size_bytes
    - 后面把该连接上的数据全部读完
    - 读完以后调 report_flow_finished(...)
    """

    def _handle_flow_conn(conn: socket.socket, addr):
        try:
            f = conn.makefile("rb")
            header = f.readline()
            if not header:
                logger.error(f"[flow_rx] empty header from {addr}")
                return
            header_json = header.decode("utf-8").strip()
            try:
                meta = json.loads(header_json)
            except json.JSONDecodeError as e:
                logger.error(f"[flow_rx] invalid header JSON from {addr}: {e}")
                return

            flow_id = int(meta.get("flow_id", 0))
            size_bytes = int(meta.get("size_bytes", 0))
            if flow_id <= 0 or size_bytes <= 0:
                logger.error(f"[flow_rx] invalid meta: {meta}")
                return

            logger.info(f"[flow_rx] start receiving flow_id={flow_id} from {addr}, "
                        f"size_bytes={size_bytes}")

            received = 0
            while True:
                chunk = f.read(4096)
                if not chunk:
                    break
                received += len(chunk)

            logger.info(f"[flow_rx] finished flow_id={flow_id}, "
                        f"received={received} bytes")

            # 告诉 RYU
            report_flow_finished(controller_ip, flow_id, received, rest_port=rest_port)

        finally:
            conn.close()

    def _server_loop():
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((listen_ip, data_port))
        server.listen(5)
        logger.info(f"[flow_rx] flow receiver listening on {listen_ip}:{data_port}")

        while True:
            conn, addr = server.accept()
            t = threading.Thread(target=_handle_flow_conn, args=(conn, addr), daemon=True)
            t.start()

    t = threading.Thread(target=_server_loop, daemon=True)
    t.start()
    return t

def post_json(url: str, payload: dict, timeout: float = 3.0) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
        if not body:
            return {}
        try:
            return json.loads(body)
        except Exception:
            return {}

def register_host_rest(ctrl_ip: str, ctrl_rest_port: int,
                       my_ip: str, permit_port: int, recv_port: int):
    url = f"http://{ctrl_ip}:{ctrl_rest_port}/scheduler/register_host"
    payload = {
        "host_ip": my_ip,
        "permit_port": permit_port,
        "recv_port": recv_port,
    }
    logging.info(f"[agent] register_host_rest: {payload}")
    resp = post_json(url, payload)
    logging.info(f"[agent] register_host_rest resp: {resp}")

def request_flow_rest(ctrl_ip: str, ctrl_rest_port: int,
                      src_ip: str, src_port: int,
                      size_bytes: int, request_rate_bps: int, priority: int = 0):
    """
    Host 发起一条业务流请求。
    目的地由 RYU 决定，不需要在这里指定。
    """
    url = f"http://{ctrl_ip}:{ctrl_rest_port}/scheduler/request"
    payload = {
        "src_ip": src_ip,
        "src_port": src_port,
        "size_bytes": size_bytes,
        "request_rate_bps": request_rate_bps,
        "priority": priority,
    }
    logging.info(f"[agent] request_flow_rest: {payload}")
    resp = post_json(url, payload)
    logging.info(f"[agent] request_flow_rest resp: {resp}")
    return resp

def demo_send_flow(ctrl_ip, ctrl_rest_port, my_ip, recv_port):
    # 这里用 recv_port 作为 src_port 只是示例，你可以自己规划端口
    size_bytes = 20_000_000
    req_rate = 5_000_000
    resp = request_flow_rest(ctrl_ip, ctrl_rest_port,
                             src_ip=my_ip,
                             src_port=recv_port,
                             size_bytes=size_bytes,
                             request_rate_bps=req_rate,
                             priority=1)
    # 返回的 resp["flow_id"] 可以记下来做调试
    return resp

# --------- 主程序入口 ----------
def main():
    """
        python host_agent.py 172.17.0.1 8080 172.17.0.101 9000 9000
    """
    if len(sys.argv) != 6:
        print("Usage: host_agent.py <ctrl_rest_ip> <ctrl_rest_port> "
              "<my_ip> <permit_port> <recv_port>")
        sys.exit(1)

    ctrl_ip = sys.argv[1]
    ctrl_rest_port = int(sys.argv[2])
    my_ip = sys.argv[3]
    permit_port = int(sys.argv[4])
    recv_port = int(sys.argv[5])

    logging.info(
        f"[agent] start: controller={ctrl_ip}:{ctrl_rest_port}, "
        f"my_ip={my_ip}, permit_port={permit_port}, recv_port={recv_port}"
    )

    # 1. 起 PERMIT server
    start_permit_server(my_ip, permit_port)

    # 2. （你可以在别处起 iperf3 -s -p recv_port 作为接收端）
    # 2. 启动“流接收器”（供其他 host 发业务流）
    #    这里示例用了 listen_port+1，你可以按需修改端口规划
    # start_flow_receiver("0.0.0.0", recv_port, recv_port)
    # 3. 向 Ryu 注册自己
    register_host_rest(ctrl_ip, ctrl_rest_port, my_ip, permit_port, recv_port)

    # 4. demo：注册一条流（实际可以根据你的业务触发）
    demo_send_flow(ctrl_ip, ctrl_rest_port, my_ip, recv_port)

    # 5. 主线程保持存活
    while True:
        time.sleep(3600)



if __name__ == "__main__":
    main()


