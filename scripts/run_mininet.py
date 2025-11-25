'''
Author: yc && qq747339545@163.com
Date: 2025-11-25 09:55:29
LastEditTime: 2025-11-25 10:06:07
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


def run():
    setLogLevel('info')
    net = Mininet(controller=RemoteController, switch=OVSSwitch, link=TCLink, autoSetMacs=True)

    c0 = net.addController('c0', controller=RemoteController,
                           ip='127.0.0.1', port=6633)

    s1 = net.addSwitch('s1')
    s2 = net.addSwitch('s2')
    s3 = net.addSwitch('s3')

    h1 = net.addHost('h1', ip='10.0.1.1/24')
    h2 = net.addHost('h2', ip='10.0.2.1/24')
    h3 = net.addHost('h3', ip='10.0.3.1/24')

    # 链路
    net.addLink(h1, s1)
    net.addLink(s1, s2)
    net.addLink(s2, s3)
    net.addLink(h2, s2)
    net.addLink(h3, s3)

    net.start()

    print("*** Topology started")
    print("*** Now you can run host_agent on h1/h2/h3, and use REST to request flows.")

    CLI(net)
    net.stop()


if __name__ == '__main__':
    run()
