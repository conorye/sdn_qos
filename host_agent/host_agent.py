'''
Author: yc && qq747339545@163.com
Date: 2025-11-25 09:53:33
LastEditTime: 2025-11-26 15:16:20
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
import logging

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


def main():
    """
    主机代理主函数
    
    使用方式:
        host_agent.py <controller_ip> <tcp_port> <my_ip>
    
    参数说明:
        controller_ip: 控制器TCP服务器的IP地址
        tcp_port: 控制器TCP服务器的端口号
        my_ip: 本主机的IP地址（用于注册）
    """
    # 参数验证
    if len(sys.argv) != 4:
        print("Usage: host_agent.py <controller_ip> <tcp_port> <my_ip>")
        print("Example: host_agent.py 127.0.0.1 9000 10.0.1.1")
        sys.exit(1)

    ctrl_ip = sys.argv[1]
    ctrl_port = int(sys.argv[2])
    my_ip = sys.argv[3]
    
    logger.info(f"Starting host agent: controller={ctrl_ip}:{ctrl_port}, my_ip={my_ip}")

    # 创建TCP socket并连接控制器
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # 设置连接超时（10秒）
        # sock.settimeout(10.0)1
        logger.info(f"Connecting to controller at {ctrl_ip}:{ctrl_port}...")
        sock.connect((ctrl_ip, ctrl_port))
        logger.info("Successfully connected to controller")
        
        # 创建文件对象用于读写
        f = sock.makefile("rwb", buffering=0)  # 无缓冲，立即刷新
        
        # 发送注册消息（JSON格式）
        register_msg = {
            "type": "REGISTER",
            "src_ip": my_ip
        }
        reg_line = (json.dumps(register_msg) + '\n').encode('utf-8')
        f.write(reg_line)
        f.flush()
        logger.info(f"Sent REGISTER message: {register_msg}")

        # 循环等待和处理控制器消息
        logger.info("Waiting for PERMIT messages from controller...")
        while True:
            try:
                line = f.readline()
                if not line:
                    logger.warning("Connection closed by controller")
                    break
                    
                line = line.decode('utf-8').strip()
                if not line:
                    continue
                    
                logger.info(f"Received message: {line}")
                
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError as e:
                    logger.error(f"Invalid JSON received: {e}, raw: {line}")
                    continue

                # 处理许可消息
                if msg.get("type") == "PERMIT":
                    handle_permit(msg)
                else:
                    logger.warning(f"Unknown message type: {msg.get('type')}")
                    
            except socket.timeout:
                # 读取超时，继续循环
                continue
            except Exception as e:
                logger.error(f"Error processing message: {e}")
                break
                
    except socket.timeout:
        logger.error(f"Connection timeout to {ctrl_ip}:{ctrl_port}")
    except ConnectionRefusedError:
        logger.error(f"Connection refused: Please check if controller is running on {ctrl_ip}:{ctrl_port}")
        logger.error("Possible solutions:")
        logger.error("1. Ensure controller is running")
        logger.error("2. Check controller IP and port")
        logger.error("3. Verify firewall settings")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
    finally:
        # 清理资源
        if sock:
            sock.close()
            logger.info("Socket connection closed")

def handle_permit(msg: dict):
    """
    处理控制器发送的流量许可消息
    
    当收到PERMIT消息时，启动iperf3生成指定参数的流量
    
    Args:
        msg: 许可消息字典，包含流参数
    """
    try:
        # 解析消息参数
        flow_id = msg.get("flow_id")
        src_ip = msg.get("src_ip")
        dst_ip = msg.get("dst_ip")
        rate_bps = int(msg.get("send_rate_bps", 0))
        size_bytes = int(msg.get("size_bytes", 0))
        dscp = msg.get("dscp", 0)  # 从消息中获取DSCP值
        
        # 参数验证
        if rate_bps <= 0 or size_bytes <= 0 or not dst_ip:
            logger.error(f"Invalid PERMIT message parameters: {msg}")
            return

        # 将DSCP转换为TOS值（DSCP左移2位）
        tos = dscp << 2
        
        # 计算流量持续时间（秒），增加10%的余量
        duration = int((size_bytes * 8 / rate_bps) * 1.1) + 1
        
        # 格式化速率字符串（Mbps或bps）
        if rate_bps >= 1_000_000:
            rate_str = f"{int(rate_bps / 1_000_000)}M"
        else:
            rate_str = f"{int(rate_bps)}"

        # 构建iperf3命令
        cmd = [
            "iperf3",
            "-u",  # UDP模式
            "-c", dst_ip,
            "-b", rate_str,
            "-t", str(duration),
            "--tos", str(tos),
            "-l", "1400",  # 设置UDP包大小
            "-i", "1",     # 每秒报告间隔
        ]
        
        logger.info(f"Starting flow_id={flow_id}, dst={dst_ip}, rate={rate_str}bps, duration={duration}s, tos={tos}")
        logger.info(f"Command: {' '.join(cmd)}")
        
        # 启动iperf3进程（后台运行）
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            logger.info(f"Started iperf3 process (PID: {process.pid}) for flow {flow_id}")
            
            # 可选：等待进程完成并记录输出
            # stdout, stderr = process.communicate()
            # if stdout:
            #     logger.info(f"iperf3 stdout: {stdout}")
            # if stderr:
            #     logger.error(f"iperf3 stderr: {stderr}")
                
        except FileNotFoundError:
            logger.error("iperf3 not found. Please install iperf3: 'sudo apt install iperf3'")
        except Exception as e:
            logger.error(f"Failed to start iperf3: {e}")
            
    except Exception as e:
        logger.error(f"Error handling PERMIT message: {e}")
# def handle_permit(msg: dict):
#     flow_id = msg.get("flow_id")
#     src_ip = msg.get("src_ip")
#     dst_ip = msg.get("dst_ip")
#     rate_bps = int(msg.get("send_rate_bps", 0))
#     size_bytes = int(msg.get("size_bytes", 0))
#     tos = int(msg.get("tos", 0))

#     if rate_bps <= 0 or size_bytes <= 0 or not dst_ip:
#         print("[agent] invalid permit:", msg)
#         return

#     # 估算持续时间（秒），留一点余量
#     duration = int(size_bytes * 8 / rate_bps) + 1
#     rate_str = f"{int(rate_bps/1_000_000)}M" if rate_bps >= 1_000_000 else f"{rate_bps}B"

#     cmd = [
#         "iperf3",
#         "-u",
#         "-c", dst_ip,
#         "-b", rate_str,
#         "-t", str(duration),
#         "--tos", str(tos),
#     ]
#     print(f"[agent] START flow_id={flow_id} cmd:", " ".join(cmd))
#     # 后台起一个进程，不等待
#     subprocess.Popen(cmd)


if __name__ == "__main__":
    main()
