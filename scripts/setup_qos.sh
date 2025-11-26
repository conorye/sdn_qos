# 原版
#!/usr/bin/env bash
###
 # @Author: yc && qq747339545@163.com
 # @Date: 2025-11-25 09:55:14
 # @LastEditTime: 2025-11-25 22:33:34
 # @FilePath: /sdn_qos/scripts/setup_qos.sh
 # @Description: 
 # 
 # Copyright (c) 2025 by ${git_name_email}, All Rights Reserved. 
### 
# scripts/setup_qos.sh
# 用 ovs-vsctl 为特定端口配置 QoS 和队列。
# 实际使用时请根据你的拓扑/端口名修改。

# set -e

# # 示例：在 s1-eth2 上配置 3 个队列
# # 你可以扩展为读取 config/qos_config.yml 自动配置

# SWITCH="s1"
# PORT="s1-eth2"
# MAX_RATE=10000000    # 10Mbps

# echo "Configuring QoS on $PORT ..."

# ovs-vsctl -- \
#   set port $PORT qos=@newqos -- \
#   --id=@newqos create qos type=linux-htb other-config:max-rate=$MAX_RATE \
#     queues:0=@q0 queues:1=@q1 queues:2=@q2 -- \
#   --id=@q0 create queue other-config:min-rate=0 other-config:max-rate=$MAX_RATE -- \
#   --id=@q1 create queue other-config:min-rate=2000000 other-config:max-rate=$MAX_RATE -- \
#   --id=@q2 create queue other-config:min-rate=5000000 other-config:max-rate=$MAX_RATE

# echo "Done."



#!/bin/bash

# 设置控制器URL
CONTROLLER_URL="http://172.17.0.1:8080"

# 交换机DPID（基于run_mininet.py创建的拓扑）
s1_dpid="0000000000000001"
s2_dpid="0000000000000002"
s3_dpid="0000000000000003"

# 设置OVSDB地址
s1_ovsdb_url="${CONTROLLER_URL}/v1.0/conf/switches/${s1_dpid}/ovsdb_addr"
s2_ovsdb_url="${CONTROLLER_URL}/v1.0/conf/switches/${s2_dpid}/ovsdb_addr"
s3_ovsdb_url="${CONTROLLER_URL}/v1.0/conf/switches/${s3_dpid}/ovsdb_addr"

echo "Setting OVSDB address for switches..."
curl -X PUT -d '"tcp:172.17.0.1:6632"' "$s1_ovsdb_url"
echo $'\n' $?
curl -X PUT -d '"tcp:172.17.0.1:6632"' "$s2_ovsdb_url"
echo $'\n' $?
curl -X PUT -d '"tcp:172.17.0.1:6632"' "$s3_ovsdb_url"
echo $'\n' $?

# 设置队列参数
s1_queue_url="${CONTROLLER_URL}/qos/queue/${s1_dpid}"
s2_queue_url="${CONTROLLER_URL}/qos/queue/${s2_dpid}"
s3_queue_url="${CONTROLLER_URL}/qos/queue/${s3_dpid}"

echo "Setting queue parameters for switches..."
# 配置s1的端口队列
curl -X POST -d '{"port_name": "s1-eth1", "type": "linux-htb", "max_rate": "1000000", "queues": [{"max_rate": "500000"}, {"min_rate": "800000"}]}' "$s1_queue_url"
echo $'\n' $?
curl -X POST -d '{"port_name": "s1-eth2", "type": "linux-htb", "max_rate": "1000000", "queues": [{"max_rate": "500000"}, {"min_rate": "800000"}]}' "$s1_queue_url"
echo $'\n' $?

# 配置s2的端口队列
curl -X POST -d '{"port_name": "s2-eth1", "type": "linux-htb", "max_rate": "1000000", "queues": [{"max_rate": "500000"}, {"min_rate": "800000"}]}' "$s2_queue_url"
echo $'\n' $?
curl -X POST -d '{"port_name": "s2-eth2", "type": "linux-htb", "max_rate": "1000000", "queues": [{"max_rate": "500000"}, {"min_rate": "800000"}]}' "$s2_queue_url"
echo $'\n' $?
curl -X POST -d '{"port_name": "s2-eth3", "type": "linux-htb", "max_rate": "1000000", "queues": [{"max_rate": "500000"}, {"min_rate": "800000"}]}' "$s2_queue_url"
echo $'\n' $?

# 配置s3的端口队列
curl -X POST -d '{"port_name": "s3-eth1", "type": "linux-htb", "max_rate": "1000000", "queues": [{"max_rate": "500000"}, {"min_rate": "800000"}]}' "$s3_queue_url"
echo $'\n' $?
curl -X POST -d '{"port_name": "s3-eth2", "type": "linux-htb", "max_rate": "1000000", "queues": [{"max_rate": "500000"}, {"min_rate": "800000"}]}' "$s3_queue_url"
echo $'\n' $?

# 设置流表规则
s1_flow_url="${CONTROLLER_URL}/qos/rules/${s1_dpid}"
s2_flow_url="${CONTROLLER_URL}/qos/rules/${s2_dpid}"
s3_flow_url="${CONTROLLER_URL}/qos/rules/${s3_dpid}"

echo "Installing flow entries to switches..."
# 配置s1的流表规则（针对目标网络10.0.3.0/24的UDP 5002端口）
curl -X POST -d '{"match": {"nw_dst": "10.0.3.0/24", "nw_proto": "UDP", "tp_dst": "5002"}, "actions":{"queue": "1"}}' "$s1_flow_url"
echo $'\n' $?
# 配置s2的流表规则
curl -X POST -d '{"match": {"nw_dst": "10.0.3.0/24", "nw_proto": "UDP", "tp_dst": "5002"}, "actions":{"queue": "1"}}' "$s2_flow_url"
echo $'\n' $?
# 配置s3的流表规则
curl -X POST -d '{"match": {"nw_dst": "10.0.3.0/24", "nw_proto": "UDP", "tp_dst": "5002"}, "actions":{"queue": "1"}}' "$s3_flow_url"
echo $'\n' $?

echo "QoS configuration completed successfully!"






# 通用脚本：根据 config/qos_config.yml 配置 QoS 和队列

#!/usr/bin/env bash

# 检查 yq 是否已安装
# if ! command -v yq &> /dev/null; then
#     echo "yq is not installed. Installing yq..."
#     # 尝试通过 Go 安装 yq
#     if command -v go &> /dev/null; then
#         go install github.com/mikefarah/yq/v4@latest
#     else
#         # 尝试通过 apt 安装
#         if command -v apt &> /dev/null; then
#             sudo apt update
#             sudo apt install -y yq
#         # 尝试通过 brew 安装
#         elif command -v brew &> /dev/null; then
#             brew install yq
#         else
#             echo "Error: yq is not installed and cannot be installed automatically."
#             echo "Please install yq manually: https://github.com/mikefarah/yq"
#             exit 1
#         fi
#     fi
# fi

# 获取 qos_config.yml 文件的路径
# CONFIG_FILE="../config/qos_config.yml"

# # 检查配置文件是否存在
# if [ ! -f "$CONFIG_FILE" ]; then
#     echo "Configuration file $CONFIG_FILE not found."
#     echo "Please create a config/qos_config.yml file with your QoS configuration."
#     exit 1
# fi

# echo "Configuring QoS based on $CONFIG_FILE..."

# # 获取所有交换机ID
# SWITCH_IDS=$(yq e 'keys | .[]' $CONFIG_FILE)

# # 遍历每个交换机ID
# for SWITCH_ID in $SWITCH_IDS; do
#     # 获取交换机ID对应的端口ID
#     PORT_IDS=$(yq e "qos_ports.\"$SWITCH_ID\" | keys | .[]" $CONFIG_FILE)
    
#     # 遍历每个端口ID
#     for PORT_ID in $PORT_IDS; do
#         # 获取端口配置
#         MAX_RATE=$(yq e "qos_ports.\"$SWITCH_ID\".\"$PORT_ID\".max_rate_bps" $CONFIG_FILE)
#         QUEUE_CONFIG=$(yq e "qos_ports.\"$SWITCH_ID\".\"$PORT_ID\".queues" $CONFIG_FILE)
        
#         # 将交换机ID转换为实际交换机名称 (s1, s2, s3)
#         SWITCH_NAME="s$SWITCH_ID"
        
#         # 将端口ID转换为实际端口名称 (s1-eth1, s1-eth2)
#         PORT_NAME="$SWITCH_NAME-eth$PORT_ID"
        
#         echo "Configuring QoS on $PORT_NAME with max_rate_bps: $MAX_RATE"
        
#         # 配置 QoS
#         # 为端口配置队列
#         QUEUE_ARGS=""
#         QUEUE_COUNT=$(yq e "qos_ports.\"$SWITCH_ID\".\"$PORT_ID\".queues | length" $CONFIG_FILE)
        
#         # 遍历每个队列
#         for i in $(seq 0 $((QUEUE_COUNT-1))); do
#             # 获取队列的最小速率
#             MIN_RATE=$(yq e "qos_ports.\"$SWITCH_ID\".\"$PORT_ID\".queues.\"$i\".min_rate_bps" $CONFIG_FILE)
            
#             # 构建队列配置参数
#             QUEUE_ARGS="$QUEUE_ARGS queues:$i=@q$i"
#             QUEUE_ARGS="$QUEUE_ARGS --id=@q$i create queue other-config:min-rate=$MIN_RATE other-config:max-rate=$MAX_RATE"
#         done
        
#         # 使用 ovs-vsctl 配置 QoS
#         echo "ovs-vsctl -- set port $PORT_NAME qos=@newqos -- --id=@newqos create qos type=linux-htb other-config:max-rate=$MAX_RATE $QUEUE_ARGS"
#         ovs-vsctl -- \
#             set port $PORT_NAME qos=@newqos -- \
#             --id=@newqos create qos type=linux-htb other-config:max-rate=$MAX_RATE \
#             $QUEUE_ARGS
        
#         echo "Done configuring $PORT_NAME"
#     done
# done

# echo "QoS configuration completed successfully!"