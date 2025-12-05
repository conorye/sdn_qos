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
from ryu import utils
from models import Flow
from path_manager import PathManager
from admission_control import AdmissionControl
from port_manager import DSCPManager,PortManager
from flow_installer import FlowInstaller
from stats_collector import StatsCollector
from host_channel import HostChannel
from exp_logger import alloc_run_id

import datetime
import os
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
       
        # ==== 实验日志系统 ====
 # ==== 新增：本次实验的时间戳 & 日志根目录 ====
        self.run_ts, self.log_root = alloc_run_id("/home/yc/sdn_qos/logs")
        self.logger.info(f"Experiment run_ts={self.run_ts}, log_root={self.log_root}")
       
       
        wsgi = kwargs['wsgi']

        # ✅ 用 register，把 self 挂到 data 里传给 SchedulerRestController
        wsgi.register(SchedulerRestController, {SCHEDULER_INSTANCE_NAME: self})
        
        # datapath 列表
        self.datapaths: Dict[int, object] = {}
        
        # flow 存储
        self.flows: Dict[int, Flow] = {}
        self.pending_flows: Dict[int, Flow] = {}
        self.active_flows: Dict[int, Flow] = {}

        # 每个源 host 单独维护计数器
        self._flow_seq_per_host: Dict[int, int] = {}   # host_no -> local seq

        

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
        port_capacity = {} # (dpid, port_no) -> capacity_bps
        ports_cfg = topo_cfg.get('ports', {})
        for dpid_str, port_map in ports_cfg.items():
            dpid = int(dpid_str, 0) if dpid_str.startswith('0x') else int(dpid_str)
            for port_no_str, cap in port_map.get('capacity_bps', {}).items():
                port_capacity[(dpid, int(port_no_str))] = int(cap)

        self.admission = AdmissionControl(port_capacity=port_capacity,log_root=self.log_root)
        self.dscp_mgr = DSCPManager()
        self.port_mgr = PortManager()
        # FlowInstaller 需要访问 self.datapaths
        self.flow_installer = FlowInstaller(self)

        # host TCP 通道
        with open(ctrl_cfg_file, 'r') as f:
            ctrl_cfg = yaml.safe_load(f) or {}
        tcp_host = ctrl_cfg.get('tcp_server_host', '0.0.0.0') # TCP服务器监听地址
        tcp_port = int(ctrl_cfg.get('tcp_server_port', 9000)) # TCP服务器监听端口
        self.logger.info(f"tcp_host:{tcp_host} tcp_port:{tcp_port}s")
        self.host_channel = HostChannel(tcp_host, tcp_port,run_ts=self.run_ts,port_mgr=self.port_mgr)
        # self.host_channel.start()

        # StatsCollector
        self.stats_collector = StatsCollector(self,self.logger, interval=1.0)
        self.stats_collector.start()

        # 调度线程
        self._scheduler_thread = hub.spawn(self._scheduler_loop)

        # REST API Controller
        # 1) 流注册接口：Host 调用
        # mapper = wsgi.mapper.
        # wsgi.registory[SCHEDULER_INSTANCE_NAME] = self
        # route_kwargs = {'scheduler_app': self}
        # mapper.connect('scheduler', BASE_URL + '/request',
        #                controller=SchedulerRestController,
        #                action='request_flow',
        #                conditions=dict(method=['POST']),
        #                **route_kwargs)
        
        # # 2) Host 注册自身信息接口：Host 调用
        # mapper.connect('scheduler', BASE_URL + '/register_host',
        #             controller=SchedulerRestController,
        #             action='register_host',
        #             conditions=dict(method=['POST']),
        #             **route_kwargs)
        
    def _delete_all_flows(self, datapath):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        match = parser.OFPMatch()
        mod = parser.OFPFlowMod(
            datapath=datapath, match=match, cookie=0,
            cookie_mask=0, table_id=ofproto.OFPTT_ALL,
            command=ofproto.OFPFC_DELETE, out_port=ofproto.OFPP_ANY,
            out_group=ofproto.OFPG_ANY)
        datapath.send_msg(mod)
    # =============== Ryu OpenFlow 事件 ===============

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        """新交换机上线时，安装 Table0 & Table2 默认规则"""
        datapath = ev.msg.datapath
        dpid = datapath.id
        self.logger.info("Switch %s connected", dpid)
        self.datapaths[dpid] = datapath
        # 先删除所有现有流表规则
        self._delete_all_flows(datapath)
        # 安装默认 pipeline
        self.logger.info(">>> install_table0_1_2_default CALLED, installing DSCP rules ...")
        self.flow_installer.install_table0_1_2_default(datapath)

    # @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    # def flow_stats_reply_handler(self, ev):
    #     """转发给 stats_collector 处理"""
    #     self.stats_collector.handle_flow_stats_reply(ev)
    #     # 这里也可以顺便调用尾部释放逻辑
    #     self._maybe_release_by_tail()
    @set_ev_cls(ofp_event.EventOFPErrorMsg, MAIN_DISPATCHER)
    def error_msg_handler(self, ev):
        msg = ev.msg
        self.logger.error(
            "OFPErrorMsg received: type=0x%02x code=0x%02x data=%s",
            msg.type, msg.code, utils.hex_array(msg.data)
    )
    
    
    
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
             size_bytes: int, priority: int,
             src_port: int = 0, dst_port: int = 0) -> Flow:
        flow_id = self._alloc_flow_id(src_ip)

        flow = Flow(
            id=flow_id,
            src_ip=src_ip,
            dst_ip=dst_ip,
            request_rate_bps=request_rate_bps,
            size_bytes=size_bytes,
            priority=priority,
            src_port=src_port,
            dst_port=dst_port,
            reason="",   # 你 Flow dataclass 里有 reason 字段，记得给默认值
        )
        self.flows[flow_id] = flow
        self.pending_flows[flow_id] = flow

        self.logger.info(
            "[scheduler] new_flow id=%d %s:%s -> %s:%s size=%d req_rate=%d priority=%d",
            flow_id, src_ip, src_port, dst_ip, dst_port, size_bytes, request_rate_bps, priority
        )
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

        # self.logger.info("[scheduler] _run_scheduler_once: pending=%d active=%d hosts=%s",len(self.pending_flows), len(self.active_flows), hosts_snapshot)
        for flow_id in list(self.pending_flows.keys()):
            flow = self.pending_flows.get(flow_id)
            if not flow:
                continue
            
        #     self.logger.info(
        #     "[scheduler] try flow id=%d src=%s dst=%s size=%d req_rate=%d prio=%d",
        #     flow.id, flow.src_ip, flow.dst_ip,
        #     getattr(flow, "size_bytes", 0),
        #     getattr(flow, "request_rate_bps", 0),
        #     getattr(flow, "priority", 0)
        # )

            path = self.path_manager.get_path(flow.src_ip, flow.dst_ip)
            if not path:
                # 找不到路径，暂时跳过
                self.logger.warning(
                "[scheduler_path] flow %d: NO PATH (%s -> %s)",
                flow.id, flow.src_ip, flow.dst_ip
                )
                continue
            # self.logger.info("[scheduler] flow %d: path=%s", flow.id, path)    
            
            ok, send_rate ,reason= self.admission.can_admit(flow, path)
            self.logger.info("[scheduler_admission] flow %d: can_admit=%s send_rate=%s reason%s",flow.id, ok, send_rate,reason)
            
            if not ok:
                continue

            # 填写调度结果
            flow.path = path
            flow.send_rate_bps = send_rate
            flow.dscp = self.dscp_mgr.alloc_dscp(flow.priority)
            # 简单映射：0->queue0, 1->queue1, 2->queue2
            flow.queue_id = flow.priority
            self.logger.info("[scheduler_dscp] flow %d: allowed, send_rate=%d dscp=%d queue_id=%d",flow.id, flow.send_rate_bps, flow.dscp, flow.queue_id
            )
            # 安装流表
            self.flow_installer.install_flow(flow)

            # 预留带宽
            self.admission.reserve(flow, path)

            # 更新状态
            flow.status = "allowed"
            flow.allowed_at = time.time()
            self.active_flows[flow.id] = flow
            del self.pending_flows[flow.id]
            self.logger.info(
            "[scheduler] flow %d: status=%s, pending=%d active=%d",
            flow.id, flow.status,
            len(self.pending_flows), len(self.active_flows)
            )
            
            flow.dst_port =self.port_mgr.alloc_dst_port(flow.dst_ip)
            flow.src_port =self.port_mgr.alloc_src_port(flow.src_ip)
            
            # 通知 host
            self.logger.info("[scheduler] flow %d: sending FLOW_PREPARE", flow.id)
            self.host_channel.send_flow_prepare(flow)  # 先通知 dst
            self.logger.info("[scheduler] flow %d: sending PERMIT", flow.id)
            self.host_channel.send_permit(flow)

    # def _maybe_release(self):
    #     """
    #     尾部检测 + 逐跳释放：
    #     - 当某 hop 的 byte_count >= size_bytes * eps 时，释放前一跳规则和带宽
    #     - 当最后一个 hop 也满足条件 / 或者长时间无新字节时，删除整条流的规则 & 释放整条路径
    #     """
    #     eps = 1.02  # 比原来的 1.05 稍微放宽一点（也可以直接用 1.0）

    #     now = time.time()

    #     for flow in list(self.s.active_flows.values()):
    #         if not flow.path:
    #             continue

    #         total = flow.size_bytes * eps

    #         # ------- 逐跳尾部释放逻辑保持不变 -------
    #         for k, (dpid, port) in enumerate(flow.path):
    #             b = flow.hop_bytes.get(dpid, 0)
    #             if b >= total and dpid not in flow.released_hops and k > 0:
    #                 prev_dpid, prev_port = flow.path[k - 1]
    #                 self.s.flow_installer.delete_prev_hop_flow(flow, prev_dpid)
    #                 self.s.admission.release_single_port(prev_dpid, prev_port, flow)

    #                 flow.released_hops.add(dpid)
    #                 msg = (f"[TailRelease] flow={flow.id} tail passed s{dpid}, "
    #                        f"release prev hop s{prev_dpid}")
    #                 self.logger.info(msg)
    #                 self._log_flow_progress(flow, [msg])

    #         # ------- 整条流是否结束？两个条件择一 -------
    #         last_dpid = flow.path[-1][0]
    #         last_bytes = flow.hop_bytes.get(last_dpid, 0)

    #         # 条件1：字节达到 size_bytes * eps（原来的机制）
    #         cond_bytes = last_bytes >= total

    #         # 条件2：最后一跳长时间没有新字节（你要的机制）
    #         idle_since = self.flow_idle_since.get(flow.id)
    #         cond_idle = idle_since is not None and \
    #                     (now - idle_since >= self.flow_idle_timeout)

    #         if (cond_bytes or cond_idle) and flow.status != "finished":
    #             flow.status = "finished"
    #             flow.finished_at = now

    #             # 额外打一次最终的 FlowProgress snapshot（status=finished）
    #             last_rate = flow.hop_rate_bps.get(last_dpid, 0)
    #             rem = max(0, flow.size_bytes - last_bytes)
    #             eta = (rem * 8 / last_rate) if last_rate > 0 else -1
    #             hop_str = " ".join(
    #                 f"s{dpid}={flow.hop_bytes.get(dpid,0)/1e6:.1f}MB"
    #                 for dpid, _ in flow.path
    #             )
    #             final_lines = [
    #                 f"[FlowProgress] flow={flow.id} class={flow.priority} dscp={flow.dscp}",
    #                 f"  sent(last_hop)={last_bytes/1e6:.2f}MB / {flow.size_bytes/1e6:.2f}MB",
    #                 f"  rate(last_hop)={last_rate/1e6:.2f}Mbps eta={eta:.1f}s",
    #                 f"  hop_bytes: {hop_str} status={flow.status}",
    #             ]
    #             self._log_flow_progress(flow, final_lines)

    #             # 删除全路径规则 & 释放资源
    #             self.s.flow_installer.delete_flow(flow)
    #             self.s.admission.release(flow)
    #             if flow.dscp is not None:
    #                 self.s.dscp_mgr.free_dscp(flow.dscp)

    #             self.s.active_flows.pop(flow.id, None)
    #             # 清理 idle 记录
    #             self.flow_idle_since.pop(flow.id, None)

    #             msg = (f"[TailRelease] flow={flow.id} finished, "
    #                    f"released all hops & freed DSCP {flow.dscp} "
    #                    f"(cond_bytes={cond_bytes}, cond_idle={cond_idle})")
    #             self.logger.info(msg)
    #             self._log_flow_progress(flow, [msg])

    
    def _alloc_flow_id(self, src_ip: str) -> int:
        """
        按源 IP 分段分配 flow_id。
        例如:
          172.17.0.101 -> host_no=1 -> 10001,10002...
          172.17.0.102 -> host_no=2 -> 20001,20002...
        """
        try:
            last_octet = int(src_ip.split(".")[-1])
        except Exception:
            # 兜底：用 0 段
            last_octet = 0

        host_no = last_octet - 100   # 101 -> 1, 102 -> 2, 103 -> 3 ...
        if host_no <= 0:
            host_no = 0

        base = host_no * 10000

        cur = self._flow_seq_per_host.get(host_no, 0) + 1
        self._flow_seq_per_host[host_no] = cur

        # 这里 +10000 是为了第一个 flow 就是 10001 / 20001 这种形态
        flow_id = base + 10000 + cur - 1
        return flow_id

# =============== REST Controller ===============

class SchedulerRestController(ControllerBase):
    def __init__(self, req, link, data, **config):
        super(SchedulerRestController, self).__init__(req, link, data,**config)
        self.scheduler_app: GlobalScheduler = data[SCHEDULER_INSTANCE_NAME]

    @route('scheduler', BASE_URL + '/request', methods=['POST'])
    def request_flow(self, req, **kwargs):
        """
        Host 发起业务流请求：
        POST /scheduler/request
        {
            "src_ip": "10.0.0.1",
            "src_port": 11000,          # Host 发流使用的本地端口（方便以后用）
            "size_bytes": 20000000,
            "request_rate_bps": 5000000,  # 可选，也可以用 qos_config 的默认
            "priority": 1
        }
        """
        try:
            body = req.text
            msg = json.loads(body)
        except Exception:
            return self._json_response({"error": "invalid json"}, status=400)

        src_ip = msg.get("src_ip")
        src_port = int(msg.get("src_port", 0))
        size_bytes = int(msg.get("size_bytes", 0))
        req_rate = int(msg.get("request_rate_bps", 0))
        priority = int(msg.get("priority", 0))

        if not src_ip or size_bytes <= 0:
            return self._json_response({"error": "invalid params"}, status=400)

        if req_rate <= 0:
            # 可以给一个默认值，或者从 qos_config 里查
            req_rate = 10_000_000  # 比如 10Mbps，按需改

        # 让 HostChannel 帮忙挑一个目的 host（随机选一个已注册 host，且 != src_ip）
        dst_info = self.scheduler_app.host_channel.pick_dst_for_flow(src_ip)
        if not dst_info:
            return self._json_response({"error": "no dst host available"}, status=503)

        dst_ip, dst_port = dst_info

        # 创建 Flow（需要在 models.Flow 里支持 src_port/dst_port，如果还没有就加上）
        flow = self.scheduler_app.new_flow(
            src_ip=src_ip,
            dst_ip=dst_ip,
            request_rate_bps=req_rate,
            size_bytes=size_bytes,
            priority=priority,
            src_port=src_port,
            dst_port=dst_port,
        )

        return self._json_response({
            "flow_id": flow.id,
            "status": flow.status,
            "dst_ip": flow.dst_ip,
            "dst_port": getattr(flow, "dst_port", None),
        })


    # def _response(self, data, status=200):
    #     body = json.dumps(data)
    #     return self._convert_response(body, status)

    # def _convert_response(self, body, status):
    #     from webob import Response
    #     return Response(content_type='application/json', body=body, status=status)
    
    def _json_response(self, data, status=200):
        from webob import Response
        body = json.dumps(data)
        return Response(
            content_type='application/json',
            charset='utf-8',                      # 告诉它编码
            body=body.encode('utf-8'),            # 这里用 bytes
            status=status
        )

    
    @route('scheduler', BASE_URL + '/register_host', methods=['POST'])
    def register_host(self, req, **kwargs):
            """
            Host 自身注册：
            POST /scheduler/register_host
            {
            "host_ip": "10.0.0.1",
            "permit_port": 10000,   # 本机上 PERMIT server 监听的端口
            "recv_port": 11000      # 本机上接收业务流的端口（比如 iperf3 -s 用的）
            }
            """
            try:
                # body = req.text
                # msg = json.loads(body)
                msg = json.loads(req.body) if req.body else {}
            except Exception:
                return self._json_response({"error": "invalid json"}, status=400)

            host_ip = msg.get("host_ip")
            permit_port = int(msg.get("permit_port", 0))
            recv_port = int(msg.get("recv_port", 0))

            if not host_ip or permit_port <= 0 or recv_port <= 0:
                return self._json_response({"error": "invalid params"}, status=400)

            # 交给 HostChannel 管理
            self.scheduler_app.host_channel.register_host(
                host_ip=host_ip,
                permit_port=permit_port,
                recv_port=recv_port,
            )

            return self._json_response({"ok": True})

