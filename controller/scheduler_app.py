# controller/scheduler_app.py
import json
import threading
import time
from typing import Dict

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, CONFIG_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.app.wsgi import WSGIApplication, ControllerBase, route
from ryu.lib import hub

from .models import Flow
from .path_manager import PathManager
from .admission_control import AdmissionControl
from .dscp_manager import DSCPManager
from .flow_installer import FlowInstaller
from .stats_collector import StatsCollector
from .host_channel import HostChannel

# REST 配置
SCHEDULER_INSTANCE_NAME = 'scheduler_api_app'
BASE_URL = '/scheduler'


class GlobalScheduler(app_manager.RyuApp):
    """
    核心 Ryu App：负责
    - 管理 flow/pending/active
    - 调度线程
    - 接入 PathManager/AdmissionControl/DSCPManager/FlowInstaller/StatsCollector/HostChannel
    """
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    _CONTEXTS = {
        'wsgi': WSGIApplication
    }

    def __init__(self, *args, **kwargs):
        super(GlobalScheduler, self).__init__(*args, **kwargs)
        wsgi = kwargs['wsgi']

        # datapath 列表
        self.datapaths: Dict[int, object] = {}

        # flow 存储
        self.flows: Dict[int, Flow] = {}
        self.pending_flows: Dict[int, Flow] = {}
        self.active_flows: Dict[int, Flow] = {}
        self._flow_id_seq = 1000

        # 初始化 PathManager / AdmissionControl / DSCPManager
        from os import path
        import yaml

        config_dir = path.join(path.dirname(path.dirname(__file__)), 'config')
        topo_cfg_file = path.join(config_dir, 'topo_config.yml')
        ctrl_cfg_file = path.join(config_dir, 'controller_config.yml')

        # PathManager 用 topo_config.yml
        self.path_manager = PathManager(topo_cfg_file)

        # 从 topo_config.yml 读取端口带宽
        with open(topo_cfg_file, 'r') as f:
            topo_cfg = yaml.safe_load(f) or {}
        port_capacity = {}
        ports_cfg = topo_cfg.get('ports', {})
        for dpid_str, port_map in ports_cfg.items():
            dpid = int(dpid_str, 0) if dpid_str.startswith('0x') else int(dpid_str)
            for port_no_str, cap in port_map.get('capacity_bps', {}).items():
                port_capacity[(dpid, int(port_no_str))] = int(cap)

        self.admission = AdmissionControl(port_capacity=port_capacity)
        self.dscp_mgr = DSCPManager()

        # FlowInstaller 需要访问 self.datapaths
        self.flow_installer = FlowInstaller(self)

        # host TCP 通道
        with open(ctrl_cfg_file, 'r') as f:
            ctrl_cfg = yaml.safe_load(f) or {}
        tcp_host = ctrl_cfg.get('tcp_server_host', '0.0.0.0')
        tcp_port = int(ctrl_cfg.get('tcp_server_port', 9000))
        self.host_channel = HostChannel(tcp_host, tcp_port)
        self.host_channel.start()

        # StatsCollector
        self.stats_collector = StatsCollector(self, interval=1.0)
        self.stats_collector.start()

        # 调度线程
        self._scheduler_thread = hub.spawn(self._scheduler_loop)

        # REST API Controller
        mapper = wsgi.mapper
        wsgi.registory[SCHEDULER_INSTANCE_NAME] = self
        route_kwargs = {'scheduler_app': self}
        mapper.connect('scheduler', BASE_URL + '/request',
                       controller=SchedulerRestController,
                       action='request_flow',
                       conditions=dict(method=['POST']),
                       **route_kwargs)

    # =============== Ryu OpenFlow 事件 ===============

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        """新交换机上线时，安装 Table0 & Table2 默认规则"""
        datapath = ev.msg.datapath
        dpid = datapath.id
        self.logger.info("Switch %s connected", dpid)
        self.datapaths[dpid] = datapath

        # 安装默认 pipeline
        self.flow_installer.install_table0_and_table2_default(datapath)

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def flow_stats_reply_handler(self, ev):
        """转发给 stats_collector 处理"""
        self.stats_collector.handle_flow_stats_reply(ev)
        # 这里也可以顺便调用尾部释放逻辑
        self._maybe_release_by_tail()

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def on_flow_stats_reply(self, ev):
        self.stats_collector.on_flow_stats(ev.msg.datapath.id, ev.msg.body)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def on_port_stats_reply(self, ev):
        self.stats_collector.on_port_stats(ev.msg.datapath.id, ev.msg.body)
    @set_ev_cls(ofp_event.EventOFPQueueStatsReply, MAIN_DISPATCHER)
    def on_queue_stats_reply(self, ev):
        self.stats_collector.on_queue_stats(ev.msg.datapath.id, ev.msg.body)



    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, CONFIG_DISPATCHER])
    def state_change_handler(self, ev):
        """维护 datapaths 字典"""
        datapath = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            if datapath.id not in self.datapaths:
                self.datapaths[datapath.id] = datapath
        elif ev.state == ofproto_v1_3.OFPCR_ROLE_SLAVE:
            if datapath.id in self.datapaths:
                del self.datapaths[datapath.id]

    # =============== Flow 管理 & 调度 ===============

    def new_flow(self, src_ip: str, dst_ip: str, request_rate_bps: int,
                 size_bytes: int, priority: int) -> Flow:
        self._flow_id_seq += 1
        flow_id = self._flow_id_seq
        flow = Flow(
            id=flow_id,
            src_ip=src_ip,
            dst_ip=dst_ip,
            request_rate_bps=request_rate_bps,
            size_bytes=size_bytes,
            priority=priority
        )
        self.flows[flow_id] = flow
        self.pending_flows[flow_id] = flow
        return flow

    def _scheduler_loop(self):
        while True:
            try:
                self._run_scheduler_once()
            except Exception:
                self.logger.exception("scheduler_loop error")
            time.sleep(1.0)

    def _run_scheduler_once(self):
        # 遍历 pending flows 尝试调度
        for flow_id in list(self.pending_flows.keys()):
            flow = self.pending_flows.get(flow_id)
            if not flow:
                continue

            path = self.path_manager.get_path(flow.src_ip, flow.dst_ip)
            if not path:
                # 找不到路径，暂时跳过
                continue

            ok, send_rate = self.admission.can_admit(flow, path)
            if not ok:
                continue

            # 填写调度结果
            flow.path = path
            flow.send_rate_bps = send_rate
            flow.dscp = self.dscp_mgr.alloc_dscp(flow.priority)
            # 简单映射：0->queue0, 1->queue1, 2->queue2
            flow.queue_id = flow.priority

            # 安装流表
            self.flow_installer.install_flow(flow)

            # 预留带宽
            self.admission.reserve(flow, path)

            # 更新状态
            flow.status = "allowed"
            flow.allowed_at = time.time()
            self.active_flows[flow.id] = flow
            del self.pending_flows[flow.id]

            # 通知 host
            self.host_channel.send_permit(flow)

    def _maybe_release_by_tail(self):
        """
        尾部检测 + 逐跳释放骨架：
        - 当某 hop 的 byte_count >= size_bytes * 1.05 时，释放前一跳规则和带宽
        - 当最后一个 hop 也满足条件时，释放全路径
        """
        for flow in list(self.active_flows.values()):
            total = flow.size_bytes
            if total <= 0 or not flow.path:
                continue
            threshold = int(total * 1.05)

            # per-hop 检查
            for idx, (dpid, port) in enumerate(flow.path):
                bytes_here = flow.hop_bytes.get(dpid, 0)
                if bytes_here >= threshold and idx > 0 and dpid not in flow.released_hops:
                    prev_dpid, prev_port = flow.path[idx - 1]
                    # 删除上一跳的规则
                    self.flow_installer.delete_prev_hop_flow(flow, prev_dpid)
                    # 释放上一跳预留
                    self.admission.release_single_port(prev_dpid, prev_port, flow)
                    flow.released_hops.add(dpid)

            # 最后一跳完成
            last_dpid, _ = flow.path[-1]
            last_bytes = flow.hop_bytes.get(last_dpid, 0)
            if last_bytes >= threshold:
                # 认为流完成
                flow.status = "finished"
                flow.finished_at = time.time()
                # 删除全路径规则
                self.flow_installer.delete_flow(flow)
                # 释放全路径预留
                self.admission.release(flow)
                # 释放 DSCP
                if flow.dscp is not None:
                    self.dscp_mgr.free_dscp(flow.dscp)
                # 从 active 移除
                del self.active_flows[flow.id]


# =============== REST Controller ===============

class SchedulerRestController(ControllerBase):
    def __init__(self, req, link, data, **config):
        super(SchedulerRestController, self).__init__(req, link, data)
        self.scheduler_app: GlobalScheduler = config['scheduler_app']

    @route('scheduler', BASE_URL + '/request', methods=['POST'])
    def request_flow(self, req, **kwargs):
        """
        业务流请求接口：
        POST /scheduler/request
        {
          "src_ip": "...",
          "dst_ip": "...",
          "request_rate_bps": 5000000,
          "size_bytes": 20000000,
          "priority": 1
        }
        """
        try:
            body = req.json if hasattr(req, 'json') else json.loads(req.body)
        except Exception:
            body = {}

        src_ip = body.get('src_ip')
        dst_ip = body.get('dst_ip')
        req_rate = int(body.get('request_rate_bps', 0))
        size_bytes = int(body.get('size_bytes', 0))
        priority = int(body.get('priority', 0))

        if not src_ip or not dst_ip or req_rate <= 0 or size_bytes <= 0:
            return self._response({"error": "invalid parameters"}, status=400)

        flow = self.scheduler_app.new_flow(src_ip, dst_ip, req_rate, size_bytes, priority)
        return self._response({
            "flow_id": flow.id,
            "status": flow.status
        })

    def _response(self, data, status=200):
        body = json.dumps(data)
        return self._convert_response(body, status)

    def _convert_response(self, body, status):
        from webob import Response
        return Response(content_type='application/json', body=body, status=status)
