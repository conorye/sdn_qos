#!/usr/bin/env bash
###
# @Author: yc && qq747339545@163.com
# @Description: 按 qos_config.yml 一次性给 OVS 配好 QoS/队列（3 队列方案）
###
# 在脚本开头添加
sudo -v  # 提前验证 sudo 权限

set -e

# -------- 0. 可选：确保 OVS 用 OpenFlow13 --------
# 如果你已经在 Mininet 里写了 protocols='OpenFlow13'，下面这段可以留着也没问题。
echo "Setting OpenFlow protocol version to OpenFlow13 on bridges s1/s2/s3..."
for br in s1 s2 s3; do
    sudo ovs-vsctl set bridge "$br" protocols=OpenFlow13 || true
done
echo "Done setting protocols."

# 如果后面不再用 Ryu 的 rest_qos 管理 OVSDB，其实不需要 set-manager。
# 如果你仍然想让 Ryu 通过 OVSDB 管理（比如以后扩展），可以保留：
# echo "Setting OVSDB manager (ptcp:6632)..."
# ovs-vsctl set-manager ptcp:6632 || true
# echo "Done setting manager."

# -------- 1. 准备 qos_config.yml --------

# 假设脚本在 project_root/scripts/setup_qos.sh
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${SCRIPT_DIR}/../config/qos_config.yml"

if [ ! -f "$CONFIG_FILE" ]; then
    echo "ERROR: QoS config file not found: $CONFIG_FILE"
    exit 1
fi

echo "Using QoS config: $CONFIG_FILE"

# 需要 yq 来解析 YAML
if ! command -v yq &> /dev/null; then
    echo "ERROR: yq is not installed."
    echo "Please install yq (https://github.com/mikefarah/yq) and rerun."
    exit 1
fi

# -------- 2. 按 qos_config.yml 为每个端口配置 3 个队列 --------
# 结构参考 qos_config.yml:
# qos_ports:
#   "1":
#     "2":
#       max_rate_bps: 10000000
#       queues:
#         "0": { min_rate_bps: 0 }
#         "1": { min_rate_bps: 2000000 }
#         "2": { min_rate_bps: 5000000 }

echo "Configuring QoS/queues according to qos_config.yml ..."

# 所有交换机 ID（dpid 简写，比如 1/2/3）
SWITCH_IDS=$(yq e '.qos_ports | keys | .[]' "$CONFIG_FILE")

for SWITCH_ID in $SWITCH_IDS; do
    # 对应交换机下的端口 ID（例如 2 -> s1-eth2）
    PORT_IDS=$(yq e ".qos_ports.\"$SWITCH_ID\" | keys | .[]" "$CONFIG_FILE")

    for PORT_ID in $PORT_IDS; do
        MAX_RATE=$(yq e ".qos_ports.\"$SWITCH_ID\".\"$PORT_ID\".max_rate_bps" "$CONFIG_FILE")
        QUEUE_COUNT=$(yq e ".qos_ports.\"$SWITCH_ID\".\"$PORT_ID\".queues | length" "$CONFIG_FILE")

        # dpid 1/2/3 -> s1/s2/s3
        SWITCH_NAME="s${SWITCH_ID}"
        # 端口号 1/2/3 -> sX-ethY
        PORT_NAME="${SWITCH_NAME}-eth${PORT_ID}"

        echo
        echo "==== Configuring $PORT_NAME (SW=$SWITCH_NAME, max_rate_bps=$MAX_RATE, queues=$QUEUE_COUNT) ===="

        # 构造 ovs-vsctl 的参数：
        #   set port $PORT_NAME qos=@newqos
        #   --id=@newqos create qos type=linux-htb other-config:max-rate=$MAX_RATE \
        #       queues:0=@q0 queues:1=@q1 ...
        #   --id=@q0 create queue other-config:min-rate=... other-config:max-rate=$MAX_RATE
        #   --id=@q1 ...
        QUEUE_REF_ARGS=""
        QUEUE_CREATE_ARGS=""

        for i in $(seq 0 $((QUEUE_COUNT-1))); do
            MIN_RATE=$(yq e ".qos_ports.\"$SWITCH_ID\".\"$PORT_ID\".queues.\"$i\".min_rate_bps" "$CONFIG_FILE")

            # queues:0=@q0 queues:1=@q1 ...
            QUEUE_REF_ARGS="$QUEUE_REF_ARGS queues:${i}=@q${i}"

            # 每个队列对应一个 queue 对象
            QUEUE_CREATE_ARGS="$QUEUE_CREATE_ARGS \
 -- --id=@q${i} create queue other-config:min-rate=${MIN_RATE} other-config:max-rate=${MAX_RATE}"
        done

        # 真正执行 ovs-vsctl
        echo "ovs-vsctl -- set port $PORT_NAME qos=@newqos -- --id=@newqos create qos type=linux-htb other-config:max-rate=$MAX_RATE $QUEUE_REF_ARGS $QUEUE_CREATE_ARGS"

        sudo ovs-vsctl -- \
            set port "$PORT_NAME" qos=@newqos -- \
            --id=@newqos create qos type=linux-htb other-config:max-rate="$MAX_RATE" \
            $QUEUE_REF_ARGS \
            $QUEUE_CREATE_ARGS

        echo "Done: $PORT_NAME"
    done
done

echo
echo "All QoS queues configured successfully."
