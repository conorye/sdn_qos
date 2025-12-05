# host_agent/host_agent.py
#!/usr/bin/env python3
import socket
import json
import subprocess

import time
import threading
import logging
import urllib.request
import subprocess

import signal
import sys
import os
import random
import numpy as np


# 保存原始路径
original_path = sys.path.copy()

# 添加项目根目录
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(project_root)

try:
    from controller.exp_logger import ExperimentLogger
    
finally:
    # 恢复原始路径
    sys.path = original_path


# 可选：记录当前 server 进程，方便 cleanup
IPERF_SERVER_PROC = None
CURRENT_CLIENT_PROC = None  # 下面第二部分会用
# BASE_LOG_DIR = "/home/yc/sdn_qos/logs"

logger = logging.getLogger("host_agent")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)


# 记录当前 host_agent 启动的所有子进程（iperf3 server / client）
CHILD_PROCS = []
CURRENT_CLIENT_PROC = None  # 文件顶部已经定义过
# 记录 server 端“即将到来”的 flow，key 用 4-tuple
# (dst_ip, dst_port, src_ip, src_port) -> (flow_id, run_ts)
PENDING_SERVER_FLOWS = {}
PENDING_SERVER_LOCK = threading.Lock()
# 记录当前 host 的 IP（在 main() 里赋值）
MY_IP = None

# 记录已经起过的 iperf3 server：port -> Popen
IPERF_SERVERS = {}
IPERF_SERVERS_LOCK = threading.Lock()

def stop_current_client():
    """
    停止当前正在发流的 iperf3 client（如果有的话）。
    """
    global CURRENT_CLIENT_PROC
    proc = CURRENT_CLIENT_PROC
    if proc is None:
        return

    if proc.poll() is None:  # 还在跑
        logging.info("[agent] stop_current_client: terminating iperf3 client pid=%s", proc.pid)
        try:
            # 先尝试优雅终止
            proc.send_signal(signal.SIGINT)  # 等价于 Ctrl+C
        except Exception:
            proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
    CURRENT_CLIENT_PROC = None

def kill_old_iperf3_server(port: int):
    """
    每次启动前调用：
    把任何形如 `iperf3 -s -p <port>` 的旧进程杀掉，避免端口占用。
    """
    pattern = f"iperf3 -s -p {port}"
    try:
        # pkill -f 会按整行 cmdline 匹配
        subprocess.run(
            ["pkill", "-f", pattern],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        logging.info(f"[agent] kill_old_iperf3_server: pkill -f '{pattern}' 已执行")
    except FileNotFoundError:
        # 没有 pkill 命令也无所谓，可以再想别的办法（lsof/fuser等）
        logging.warning("[agent] pkill 不存在，无法自动清理旧 iperf3 server")

# --------- PERMIT 推送接收 Server ----------

def handle_permit(msg: dict):
    """
    收到 PERMIT 后，启动 iperf3 发送流（只起一个 client），
    并把输出重定向到 client.log，同时在前后写时间戳。
    """
    global CURRENT_CLIENT_PROC

    flow_id = msg.get("flow_id")
    src_ip = msg.get("src_ip")
    dst_ip = msg.get("dst_ip")
    dst_port = int(msg.get("dst_port", 0)) or None
    src_port = int(msg.get("src_port", 0)) or None    # 新增：指定 client 端口
    rate_bps = int(msg.get("send_rate_bps", 0))
    size_bytes = int(msg.get("size_bytes", 0))
    dscp = int(msg.get("dscp", 0))
    run_ts = msg.get("run_ts")

    if rate_bps <= 0 or size_bytes <= 0 or not dst_ip:
        logger.error(f"Invalid PERMIT: {msg}")
        return

    # DSCP -> TOS
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
    if dst_port is not None:
        cmd += ["-p", str(dst_port)]
    if src_port is not None:
        cmd += ["--cport", str(src_port)]   # 关键：让 iperf3 用指定 src_port
        
    # ===== 统一 client 日志命名 =====
    # /home/yc/sdn_qos/logs/<run_id>/iperf/<flow_id>:<src_ip>_to_<dst_ip>/client.log
    exp_logger = ExperimentLogger(
        base_dir="/home/yc/sdn_qos/logs",
        run_id=run_ts,
    )
    iperf_dir = exp_logger.run_dir / "iperf"
    iperf_dir.mkdir(parents=True, exist_ok=True)

    # 新增：为每个 flow 建一个子目录
    flow_dir = iperf_dir / f"{flow_id}:{src_ip}_to_{dst_ip}"
    flow_dir.mkdir(parents=True, exist_ok=True)

    # 日志文件改成固定叫 client.log
    client_log_path = flow_dir / "client.log"

    logger.info(f"[agent] START flow_id={flow_id} cmd: {' '.join(cmd)} "
                f"log={client_log_path}")
    start_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

    with open(client_log_path, "w") as f:
        f.write(f"=== iperf3 client START {start_ts} ===\n")
        f.write(f"CMD: {' '.join(cmd)}\n\n")
        f.flush()

        proc = subprocess.Popen(
            cmd,
            stdout=f,
            stderr=subprocess.STDOUT,
        )

    CHILD_PROCS.append(proc)
    CURRENT_CLIENT_PROC = proc

    def _wait_and_mark(p, log_path):
        rc = p.wait()
        end_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        with open(log_path, "a") as ff:
            ff.write(f"\n=== iperf3 client END {end_ts}, rc={rc} ===\n")

    threading.Thread(
        target=_wait_and_mark,
        args=(proc, client_log_path),
        daemon=True,
    ).start()
    
def handle_flow_prepare(msg: dict):
    """
    目的 host 收到 FLOW_PREPARE：
    记录 (dst_ip, dst_port, src_ip, src_port) -> (flow_id, run_ts)，
    供后面 iperf3 server 日志拆分、以及从端口反查 flow_id 使用。
    """
    global MY_IP
    flow_id = int(msg.get("flow_id", 0) or 0)
    src_ip = msg.get("src_ip")
    dst_ip = msg.get("dst_ip")
    src_port = int(msg.get("src_port", 0) or 0)
    dst_port = int(msg.get("dst_port", 0) or 0)
    run_ts = msg.get("run_ts")

    if not (flow_id and src_ip and dst_ip and src_port and dst_port):
        logger.error(f"[agent] Invalid FLOW_PREPARE: {msg}")
        return

    key = (dst_ip, dst_port, src_ip, src_port)

    with PENDING_SERVER_LOCK:
        PENDING_SERVER_FLOWS[key] = (flow_id, run_ts)

    logger.info("[agent] FLOW_PREPARE: key=%s -> flow_id=%s run_ts=%s",
                key, flow_id, run_ts)
    
        # 关键：根据 FLOW_PREPARE 的 dst_port 启动（或复用）对应端口的 iperf3 server
    if MY_IP is not None:
        start_iperf3_server(MY_IP, dst_port)
    else:
        logger.warning("[agent] MY_IP is None, cannot start iperf3 server for FLOW_PREPARE")


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
                    
                elif msg_type == "FLOW_PREPARE":
                    handle_flow_prepare(msg)
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
    （scheduler_app 里如果暂时没实现 /scheduler/host_report，这个可以先不用）
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


def start_iperf3_server(my_ip, listen_port: int):
    """
    在本机启动 iperf3 server：
      - 监听 listen_port；
      - 如果该端口已有存活的 iperf3 server，则复用，不再重复启动；
      - 解析 stdout 中的每条 connection/test；
      - 根据 (dst_ip, dst_port, src_ip, src_port) 到 PENDING_SERVER_FLOWS 里
        查出 flow_id、run_ts；
      - 用 ExperimentLogger 把每次 test 写到：
          <run_id>/iperf/<flow_id>:server_<src_ip>_to_<my_ip>.log
    """
    global IPERF_SERVER_PROC, IPERF_SERVERS

    with IPERF_SERVERS_LOCK:
        # 如果这个端口已经有活着的 server，就直接复用
        old = IPERF_SERVERS.get(listen_port)
        if old is not None and old.poll() is None:
            logging.info(f"[agent] iperf3 server already running on {listen_port}, reuse it")
            return old

        # 如有需要可以提前杀掉旧的 iperf3 server
        # kill_old_iperf3_server(listen_port)

        cmd = ["iperf3", "-s", "-p", str(listen_port)]
        logging.info(f"[agent] start iperf3 server: {' '.join(cmd)}")

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        IPERF_SERVER_PROC = proc
        IPERF_SERVERS[listen_port] = proc
        CHILD_PROCS.append(proc)

    def _pump_server_output(p: subprocess.Popen, my_ip: str, listen_port: int):
        current_seg_f = None
        current_flow_id = None
        current_run_ts = None
        current_src_ip = None

        for line in p.stdout:
            line_stripped = line.rstrip("\n")
            logger.info("[iperf3-server] %s", line_stripped)

            # 1) 新的连接开始
            if "Accepted connection from" in line_stripped:
                try:
                    if "connected to" in line_stripped:
                        parts = line_stripped.split("connected to", 1)[1].split()
                        src_ip = parts[0]
                        src_port = int(parts[2])
                    else:
                        tokens = line_stripped.split("Accepted connection from", 1)[1].split(",")
                        src_ip = tokens[0].strip()
                        src_port = None
                except Exception:
                    src_ip = None
                    src_port = None

                if current_seg_f is not None:
                    current_seg_f.write("\n=== TEST END ===\n")
                    current_seg_f.close()
                    current_seg_f = None

                current_flow_id = None
                current_run_ts = None
                current_src_ip = src_ip

            elif "connected to" in line_stripped:
                try:
                    seg = line_stripped.split("connected to", 1)[1].strip()
                    parts = seg.split()
                    src_ip = parts[0]
                    src_port = int(parts[2])
                except Exception:
                    src_ip = None
                    src_port = None

                dst_ip = my_ip
                dst_port = listen_port

                flow_id = None
                run_ts = None
                if src_ip is not None and src_port is not None:
                    key = (dst_ip, dst_port, src_ip, src_port)
                    with PENDING_SERVER_LOCK:
                        flow_info = PENDING_SERVER_FLOWS.pop(key, None)
                    if flow_info is not None:
                        flow_id, run_ts = flow_info

                if flow_id is not None and run_ts is not None:
                    exp_logger = ExperimentLogger(
                        base_dir="/home/yc/sdn_qos/logs",
                        run_id=run_ts,
                    )
                    server_log_path = exp_logger.iperf_server_log_path(
                        flow_id=flow_id,
                        src_ip=src_ip,
                        dst_ip=dst_ip,
                    )
                    start_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
                    current_seg_f = open(server_log_path, "w")
                    current_seg_f.write(f"=== iperf3 server TEST START {start_ts} ===\n")
                    current_seg_f.write(line_stripped + "\n")
                    current_seg_f.flush()

                    current_flow_id = flow_id
                    current_run_ts = run_ts
                    current_src_ip = src_ip
                else:
                    current_seg_f = None
                    current_flow_id = None
                    current_run_ts = None
                    current_src_ip = None

            else:
                if current_seg_f is not None:
                    current_seg_f.write(line_stripped + "\n")
                    current_seg_f.flush()

        if current_seg_f is not None:
            end_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            current_seg_f.write(f"\n=== TEST END {end_ts} ===\n")
            current_seg_f.close()

    t = threading.Thread(
        target=_pump_server_output,
        args=(proc, my_ip, listen_port),
        daemon=True,
    )
    t.start()

    return proc






def start_flow_receiver(listen_ip: str, data_port: int,
                        controller_ip: str, rest_port: int = 8080):
    """
    简化版“流接收器”骨架：
    - 接受一个连接
    - 第一行 JSON 说明 flow_id / size_bytes
    - 后面把该连接上的数据全部读完
    - 读完以后调 report_flow_finished(...)
    （如果你只用 iperf3，可以暂时不用这个）
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


# --------- REST 辅助函数 ----------

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


def experiment_loop(ctrl_ip: str, ctrl_rest_port: int, my_ip: str, default_src_port: int, max_flows: int, max_interval: float, lambda_val: float):
    """
    启动一个随机实验，生成指数时间间隔的流量。
    
    参数：
    - max_flows: 最大生成的随机流量数目。
    - max_interval: 流量之间的最大间隔。
    - lambda_val: 指数分布的λ值。
    """
    print("\n=== 开始实验 ===")
    print(f"将生成最多 {max_flows} 个随机流量，并使用指数间隔。")
    
    for _ in range(max_flows):
        # 随机生成流量参数
        size_bytes = random.randint(1_000_000, 10_000_000)  # 随机流量大小（1MB 到 10MB）
        rate_bps = random.randint(1_000_000, 10_000_000)  # 随机速率（1Mbps 到 10Mbps）
        priority = random.choice([0, 1, 2])  # 随机优先级（0、1或2）
        
        # 使用指数分布生成流量间隔
        interval = np.random.exponential(1 / lambda_val)
        
        # 发起流量请求
        request_flow(ctrl_ip, ctrl_rest_port, my_ip, default_src_port, size_bytes, rate_bps, priority)
        
        # 记录实验详情
        print(f"生成的流量：大小={size_bytes}字节，速率={rate_bps} bps，优先级={priority}，间隔={interval:.2f}秒")
        
        # 根据生成的间隔休眠
        time.sleep(min(interval, max_interval))  # 不超过最大间隔

def cleanup():
    """
    杀掉当前 host_agent 启动的所有子进程（iperf3 server / client），
    不影响别的 xterm / host，因为只操作 CHILD_PROCS 列表里的 Popen 对象。
    """
    logger.info("[agent] cleanup: terminating child processes...")
    for p in CHILD_PROCS:
        if p.poll() is None:  # 还在跑
            try:
                logger.info(f"[agent] terminate pid={p.pid}")
                p.terminate()
            except Exception as e:
                logger.warning(f"[agent] terminate pid={p.pid} failed: {e}")

    # 给它们一点时间自己退出
    time.sleep(0.5)

    # 如果还有没退出的，强制 kill
    for p in CHILD_PROCS:
        if p.poll() is None:
            try:
                logger.info(f"[agent] kill pid={p.pid}")
                p.kill()
            except Exception as e:
                logger.warning(f"[agent] kill pid={p.pid} failed: {e}")
    logger.info("[agent] cleanup done.")
# --------- 交互式 CLI ----------

def cli_loop(ctrl_ip: str, ctrl_rest_port: int,
             my_ip: str, default_src_port: int):
    """
    简单 CLI：
      flow <size_MB> <rate_Mbps> [priority]
      quit / exit
    """
    print("\n=== host_agent CLI 已启动 ===")
    print("命令示例：")
    print("  flow 20 5 1    # 20MB, 5Mbps, priority=1")
    print("  flow 10 1      # 10MB, 1Mbps, priority 默认=1")
    print("  quit           # 退出 CLI\n")
    
    while True:
        try:
            line = input("host_agent> ").strip()
        except EOFError:
            # Ctrl-D
            print("\nEOF，退出 CLI")
            break

        if not line:
            continue

        if line in ("quit", "exit"):
            print("bye")
            break

        parts = line.split()
        cmd = parts[0]

        if cmd == "flow":
            if len(parts) < 3:
                print("用法: flow <size_MB> <rate_Mbps> [priority]")
                continue
            try:
                size_mb = float(parts[1])
                rate_mbps = float(parts[2])
                priority = int(parts[3]) if len(parts) >= 4 else 1
            except ValueError:
                print("参数必须是数字，如: flow 20 5 1")
                continue

            size_bytes = int(size_mb * 1024 * 1024)
            req_rate = int(rate_mbps * 1_000_000)

            try:
                resp = request_flow_rest(
                    ctrl_ip, ctrl_rest_port,
                    src_ip=my_ip,
                    src_port=default_src_port,
                    size_bytes=size_bytes,
                    request_rate_bps=req_rate,
                    priority=priority,
                )
                print("controller 返回:", resp)
        

            except KeyboardInterrupt:
                # 用户在“发流过程中”按了 Ctrl+C
                print("\n检测到 Ctrl+C，停止当前 iperf3 client，但保留 CLI")
                stop_current_client()
                # 不 raise，让 CLI 继续
                continue
        else:
            print("未知命令，支持: flow / quit / exit")


# --------- 主程序入口 ----------

def main():
    """
        示例：
        python host_agent.py 172.17.0.1 8080 10.0.0.1 10000 9000
        对应：
          ctrl_rest_ip   = 172.17.0.1
          ctrl_rest_port = 8080
          my_ip          = 10.0.0.1   (host 在 Mininet 里的 IP)
          permit_port    = 10000      (本机 PERMIT server 监听端口)
          recv_port      = 9000       (你起 iperf3 -s -u -p 的端口)
          permit_port 和 recv_port 一定要用不同的端口
          
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
    global MY_IP
    MY_IP = my_ip
    
    
    logging.info(
        f"[agent] start: controller={ctrl_ip}:{ctrl_rest_port}, "
        f"my_ip={my_ip}, permit_port={permit_port}, recv_port={recv_port}"
    )

    # 1. 起 PERMIT server（后台线程）
    start_permit_server(my_ip, permit_port)

    # 2. （建议在另一个终端里自己起 iperf3 -s -u -p recv_port）
    #    例如： iperf3 -s -u -p 9000
    # start_iperf3_server(my_ip,recv_port)
    # 3. 向 Ryu 注册自己（登记 host_ip / permit_port / recv_port）
    register_host_rest(ctrl_ip, ctrl_rest_port, my_ip, permit_port, recv_port)

    # 4. 进入 CLI，由你手动触发业务流请求
    # cli_loop(ctrl_ip, ctrl_rest_port, my_ip, recv_port)

    # 5. CLI 退出后，如果你希望进程继续挂着，可以改成 while True:sleep(...)
    try:
        cli_loop(ctrl_ip, ctrl_rest_port, my_ip, recv_port)
    except KeyboardInterrupt:
        print("\n检测到 Ctrl+C（main），正在清理资源并退出...")
        cleanup()
    finally:
        cleanup()
        
        logging.info("[agent] host_agent 结束")


if __name__ == "__main__":
    main()
