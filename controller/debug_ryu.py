'''
Author: yc && qq747339545@163.com
Date: 2025-08-23 20:40:57
LastEditTime: 2025-11-25 21:17:40
FilePath: /sdn_qos/controller/debug_ryu.py
Description: 

Copyright (c) 2025 by ${git_name_email}, All Rights Reserved. 
'''

import os
import sys
from ryu.cmd import manager

# 设置环境变量
# os.environ['ALGO'] = 'DIJKSTRA'
#  ./ryu_qos_apps/rest_conf_switch.py ./ryu_qos_apps/rest_qos.py  ./ryu_qos_appsqos_simple_switch_13.py


# 添加命令行参数
sys.argv.extend([
    'scheduler_app',  # 你的应用路径 network_traffic_detect  main drl_forwarding
    '../ryu_qos_apps/rest_conf_switch','../ryu_qos_apps/rest_qos','../ryu_qos_appsqos_simple_switch_13'
    '--enable-debugger'  # 可选：Ryu 内置调试支持
])

# 启动 Ryu manager
manager.main()
