'''
Author: yc && qq747339545@163.com
Date: 2025-08-23 20:40:57
LastEditTime: 2025-09-23 12:26:14
FilePath: /25.9.Deploy_DRL-TP2/application_mode/debug_ryu.py
Description: 

Copyright (c) 2025 by ${git_name_email}, All Rights Reserved. 
'''

import os
import sys
from ryu.cmd import manager

# 设置环境变量
# os.environ['ALGO'] = 'DIJKSTRA'

# 添加命令行参数
sys.argv.extend([
    'network_traffic_detect',  # 你的应用路径 network_traffic_detect  main drl_forwarding
    'drl_forwarding',
    '--observe-links',
    '--k-paths=3',
    # '--verbose',  # 可选：启用详细日志
    '--algo=DRL',  # 使用 DRL 算法
    '--enable-debugger'  # 可选：Ryu 内置调试支持
])

# 启动 Ryu manager
manager.main()
