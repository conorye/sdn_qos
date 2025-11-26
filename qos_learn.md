ryu：
ryu-manager ./ryu_qos_apps/rest_conf_switch.py ./ryu_qos_apps/rest_qos.py  ./ryu_qos_appsqos_simple_switch_13.py

ryu-manager --ofp-tcp-listen-port 6633 --ofp-listen-host 172.17.0.1 --wsapi-host 172.17.0.1 --wsapi-port 8080 ./ryu_qos_apps/rest_conf_switch.py ./ryu_qos_apps/rest_qos.py ./ryu_qos_apps/qos_simple_switch_13.py ./controller/scheduler_app.py



mininet:

sudo mn --custom  ./topology/datacenterBasic.py         --topo dcbasic         --controller=remote,ip=192.168.1.100,port=6633         --switch ovsk,protocols=OpenFlow13         --link tc

命令：

sh ovs-vsctl show

sh ovs-ofctl dump-flows s1

dpctl dump-flows

 dpctl dump-ports

dpctl  del-flows

sudo ovs-appctl qos/show s1-eth1

```
QoS: s1-eth1 linux-htb
max-rate: 1000000

Default:
  burst: 12512
  min-rate: 12000
  max-rate: 500000
  tx_packets: 14
  tx_bytes: 2177
  tx_errors: 0

Queue 1:
  burst: 12512
  min-rate: 800000
  max-rate: 1000000
  tx_packets: 0
  tx_bytes: 0
  tx_errors: 0
```



yc@yc-TUF-2:~/sdn_qos/scripts$ sudo ovs-appctl dpif/show 

```
system@ovs-system: hit:109 missed:37
  s1:
    s1 65534/3: (internal)
    s1-eth1 1/2: (system)
    s1-eth2 2/1: (system)
  s2:
    s2 65534/7: (internal)
    s2-eth1 1/6: (system)
    s2-eth2 2/5: (system)
    s2-eth3 3/4: (system)
  s3:
    s3 65534/10: (internal)
    s3-eth1 1/8: (system)
    s3-eth2 2/9: (system)
```



Q:
检查我整合的工程代码是否有问题,是否符合设计文档中的需求

这里需要补写一个日志类

希望提供一个完整的执行手册

我现在的调试思路是：

“实验 A：单条业务流（验证全链路功能）

h1 → h3

priority = Silver

request_rate = 5 Mbps

size = 30 MB

预期观测：

pending → allowed → active → finished

FlowStats 有进度/ETA

尾部到达 s2/s3 后逐跳释放 s1/s2 的流表与预留

实验 B：两条业务并发（验证全局带宽调度）

flow1: h1 → h3, priority=Gold, req=6Mbps, size=40MB

flow2: h1 → h3, priority=Silver, req=6Mbps, size=40MB

链路 10 Mbps，两个 6M 会超订

预期：

Gold 先 admit（或给更多 send_rate）

Silver pending，等 Gold 尾部释放后被 admit

你能看到 pending 队列里 Silver 的等待时间

实验 C：背景流压测（验证多队列 QoS 护航）

按实验 A 或 B 的业务流照跑

同时 h2 → h3（或 h2 → h1）启动无报备 UDP 背景流 20 Mbps

预期：

背景流走 Table2 → Best-effort 队列

业务流走 Table1 → Gold/Silver 队列

拥塞时 Gold/Silver 的速率/完成时间明显好于无队列时”
完成这3个实验。若报错则一步步写详细的调试、日志函数 一步步地看问题出现在哪里，直到调通为止



Q2:
“2) 配置 QoS 队列（scripts/setup_qos.sh）

示例（假设 OVS bridge 名称为 s1、s2、s3 的端口命名需按 mininet 实际端口来）：

# Example: set queues on s1-eth2 (s1->s2), s2-eth3 (s2->s3) -- adjust port names accordingly
sudo ovs-vsctl -- set Port s1-eth2 qos=@newqos -- \
  --id=@newqos create QoS type=linux-htb other-config:max-rate="10000000" \
  queues:0=@q0 queues:1=@q1 queues:2=@q2 \
  -- --id=@q0 create Queue other-config:min-rate=0 \
  -- --id=@q1 create Queue other-config:min-rate=2000000 \
  -- --id=@q2 create Queue other-config:min-rate=5000000
# repeat similarly for s2-eth3 (s2->s3)


注意：Mininet 的端口名要根据实际 ifconfig / ovs-vsctl show 输出调整。”

如何将这个转换成我发的










推进：


additional：
flow 表字段和端口带宽 / max_rate 的 API 详细说明,比如流表匹配原则，go_to_table的含义

"上报“将要发的流”：/scheduler/plan"包含rate_bps应当分装成该任务的优先级，以及时间敏感度，综合转换成的rate_bps对于带宽的需要


“ip_dscp（ToS）：你可以当作“业务等级 / 通道 ID / 子流标记”。

同一个业务可以有多个 dscp，对应不同路径 / 队列；

所有 switch 都可以用 ip_dscp 匹配，把它送去不同的 queue / 端口。

ip_ecn：主要和拥塞控制相关（ECN 标记），例如在队列快满时给包打 ECN bit，让端系统降速。你现在如果不搞主动队列管理，可以先不管。”


max-rate：不允许超过的最高速率（流量整形）
是直接丢包吗?