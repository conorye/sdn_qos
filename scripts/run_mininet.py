'''
Author: yc && qq747339545@163.com
Date: 2025-11-25 09:55:29
LastEditTime: 2025-11-26 12:03:01
FilePath: /sdn_qos/scripts/run_mininet.py
Description: 

Copyright (c) 2025 by ${git_name_email}, All Rights Reserved. 
'''
#!/usr/bin/env python3
# scripts/run_mininet.py
#
# 简单示例拓扑：
# h1 - s1 - s2 - s3 - h3
# 中间还有一个 h2 接在 s2 上，方便做背景流等。

from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.nodelib import NAT  

def run():
    setLogLevel('info')
    net = Mininet(controller=RemoteController, switch=OVSSwitch, link=TCLink, autoSetMacs=True)

    c0 = net.addController('c0', controller=RemoteController,
                           ip='172.17.0.1', port=6633) # 192.168.1.100 127.0.0.1  172.17.0.1

    s1 = net.addSwitch('s1')
    s2 = net.addSwitch('s2')
    s3 = net.addSwitch('s3')

    h1 = net.addHost('h1', ip='172.17.0.101/24',defaultRoute='via 172.17.0.254')
    h2 = net.addHost('h2', ip='172.17.0.102/24',defaultRoute='via 172.17.0.254')
    h3 = net.addHost('h3', ip='172.17.0.103/24',defaultRoute='via 172.17.0.254')

    # NAT 节点：连接 Mininet 内网和“外部世界”（宿主机网络）
    # inNamespace=False 表示它跟宿主机在一个 namespace 里，便于做 iptables
    nat1 = net.addHost('nat1', cls=NAT, ip='172.17.0.254/24', inNamespace=False)
    # nat2 = net.addHost('nat2', cls=NAT, ip='172.17.0.254/24', inNamespace=False)
    # nat3 = net.addHost('nat3', cls=NAT, ip='172.17.0.254/24', inNamespace=False)
    # 链路
    net.addLink(h1, s1)
    net.addLink(s1, s2)
    net.addLink(s2, s3)
    net.addLink(h2, s2)
    net.addLink(h3, s3)
    net.addLink(nat1, s1)   # NAT 挂在 s1 上，你也可以挂在别的交换机
    # net.addLink(nat2, s2) 
    # net.addLink(nat3, s3) 
    
    net.start()

    print("*** Topology started")
    print("*** Now you can run host_agent on h1/h2/h3, and use REST to request flows.")

    CLI(net)
    net.stop()


if __name__ == '__main__':
    run()
