'''
Author: yc && qq747339545@163.com
Date: 2025-11-25 17:22:18
LastEditTime: 2025-11-25 17:27:57
FilePath: /sdn_qos/controller/main.py
Description: 

Copyright (c) 2025 by ${git_name_email}, All Rights Reserved. 
'''
#!/usr/bin/env python3
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.app.wsgi import ControllerBase, WSGIApplication, route
from webob import Response

# 导入你的组件
from dscp_manager import DSCPManager
from path_manager import PathManager
from admission_control import AdmissionControl
from flow_installer import FlowInstaller
from scheduler_app import GlobalScheduler
from stats_collector import StatsCollector

class SDNQosController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    
    def __init__(self, *args, **kwargs):
        super(SDNQosController, self).__init__(*args, **kwargs)
        
        # 初始化组件
        self.dscp_mgr = DSCPManager()
        self.path_mgr = PathManager()
        self.admission = AdmissionControl()
        self.flow_installer = FlowInstaller()
        self.stats_collector = StatsCollector()
        self.scheduler = GlobalScheduler(self.dscp_mgr, self.path_mgr, self.admission, self.flow_installer,self.stats_collector )
        
        
        # 启动调度器
        self.scheduler.start()
        
        # 启动统计收集器
        self.stats_collector.start()
        
        # 添加路由
        wsgi = kwargs['wsgi']
        wsgi.register(SdnQosApi, {'sdn_qos_controller': self})
        
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        """处理交换机连接事件"""
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        
        # 安装默认流表项
        self.flow_installer.install_default_flows(datapath)
        
        # 初始化交换机
        self.path_mgr.add_switch(datapath.id)
        
        # 启动调度器
        self.scheduler.start()