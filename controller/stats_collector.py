'''
Author: yc && qq747339545@163.com
Date: 2025-11-25 09:51:25
LastEditTime: 2025-11-25 22:14:00
FilePath: /sdn_qos/controller/stats_collector.py
Description:   Stats 收集模块

Copyright (c) 2025 by ${git_name_email}, All Rights Reserved. 
'''
# controller/stats_collector.py
import threading
import time
from typing import Dict
from ryu.ofproto import ofproto_v1_3
from models import Flow


class StatsCollector:
    """
    周期性向交换机请求 FlowStats，并更新 Flow 的进度信息。
    需要 GlobalScheduler 提供：
      - datapaths: dpid -> datapath
      - flows: flow_id -> Flow
    """

    def __init__(self, scheduler, logger ,interval: float = 1.0):
        self.s = scheduler
        self.interval = interval
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._running = False
        self.logger = logger
        self.last_book_dump = 0
        self.last_queue_dump = 0

    def start(self):
        self._running = True
        self._thread.start()

    def _loop(self):
        while self._running:
            try:
                self.request_all()
            except Exception:
                pass
            time.sleep(self.interval)

    def request_all(self):
        for dp in self.s.datapaths.values():
            self._req_flow(dp)
            self._req_port(dp)
            self._req_queue(dp)

    
    def _req_flow(self, dp):
        parser = dp.ofproto_parser
        dp.send_msg(parser.OFPFlowStatsRequest(dp))

    def _req_port(self, dp):
        parser = dp.ofproto_parser
        dp.send_msg(parser.OFPPortStatsRequest(dp, 0, dp.ofproto.OFPP_ANY))

    def _req_queue(self, dp):
        parser = dp.ofproto_parser
        dp.send_msg(parser.OFPQueueStatsRequest(dp, 0, dp.ofproto.OFPP_ANY, dp.ofproto.OFPQ_ALL))
    
    # def _poll_stats(self):
    #     for dp in list(self.app.datapaths.values()):
    #         ofp = dp.ofproto
    #         parser = dp.ofproto_parser
    #         # 请求所有表的 FlowStats
    #         req = parser.OFPFlowStatsRequest(dp, 0, ofp.OFPTT_ALL,
    #                                          ofp.OFPP_ANY, ofp.OFPG_ANY,
    #                                          0, 0, parser.OFPMatch())
    #         dp.send_msg(req)
            
            
    def on_flow_stats(self, dpid, stats):
        now = time.time()
        for st in stats:
            fid = (st.cookie >> 32) & 0xffffffff
            if fid==0: 
                continue
            flow = self.s.active_flows.get(fid)
            if not flow:
                continue

            prev_bytes = flow.hop_bytes.get(dpid, 0)
            prev_t = flow.hop_last_time.get(dpid, now)
            delta_b = st.byte_count - prev_bytes
            delta_t = max(1e-3, now - prev_t)
            rate_bps = int(delta_b * 8 / delta_t)

            flow.hop_bytes[dpid] = st.byte_count
            flow.hop_last_time[dpid] = now
            flow.hop_rate_bps[dpid] = rate_bps

        self._print_flow_progress()
        # self._maybe_release()

        if now - self.last_book_dump > 3:
            self._print_port_book()
            self.last_book_dump = now

    def _print_flow_progress(self):
        for flow in self.s.active_flows.values():
            last_dpid = flow.path[-1][0]
            sent = flow.hop_bytes.get(last_dpid, 0)
            rate = flow.hop_rate_bps.get(last_dpid, 0)
            total = flow.size_bytes
            rem = max(0, total - sent)
            eta = (rem*8/rate) if rate>0 else -1

            hop_str = " ".join([f"s{dpid}={flow.hop_bytes.get(dpid,0)/1e6:.1f}MB"
                                for dpid,_ in flow.path])

            self.logger.info(
                f"[FlowProgress] flow={flow.id} class={flow.priority} dscp={flow.dscp}\n"
                f"  sent(last_hop)={sent/1e6:.2f}MB / {total/1e6:.2f}MB\n"
                f"  rate(last_hop)={rate/1e6:.2f}Mbps eta={eta:.1f}s\n"
                f"  hop_bytes: {hop_str} status={flow.status}"
            )

    def _maybe_release(self):
        eps = 1.05
        for flow in list(self.s.active_flows.values()):
            total = flow.size_bytes * eps
            for k,(dpid,port) in enumerate(flow.path):
                b = flow.hop_bytes.get(dpid,0)
                if b >= total and dpid not in flow.released_hops and k>0:
                    prev_dpid, prev_port = flow.path[k-1]
                    dp_prev = self.s.datapaths.get(prev_dpid)
                    if dp_prev:
                        self.s.flow_installer.delete_flow_on_dp(dp_prev, flow.id)
                    self.s.adm.release_one(prev_dpid, prev_port, flow.send_rate_bps)
                    flow.released_hops.add(dpid)
                    self.logger.info(
                        f"[TailRelease] flow={flow.id} tail passed s{dpid}, "
                        f"release prev hop s{prev_dpid}"
                    )

            # last hop finished -> mark & cleanup
            last_dpid = flow.path[-1][0]
            if flow.hop_bytes.get(last_dpid,0) >= total and flow.status!="finished":
                flow.status="finished"
                flow.finished_at=time.time()
                self.s.finish_flow(flow)

    def _print_port_book(self):
        for dpid,port,cap,res,avail in self.s.admission.dump_book():
            self.logger.info(f"[PortBook] (s{dpid},{port}) cap={cap/1e6:.1f}M "
                             f"reserved={res/1e6:.1f}M avail={avail/1e6:.1f}M")
    

    def on_port_stats(self, dpid, stats):
        # Demo可选：打印端口利用率
        pass

    def on_queue_stats(self, dpid, stats):
        # Demo可选：打印队列吞吐
        pass    

##原来的逻辑
    def handle_flow_stats_reply(self, ev):
        """在 scheduler_app 中调用，用来处理 EventOFPFlowStatsReply"""
        msg = ev.msg
        dp = msg.datapath
        body = msg.body
        now = time.time()

        for stat in body:
            cookie = stat.cookie
            flow_id = (cookie >> 32) & 0xffffffff
            if flow_id == 0:
                # 0 视为系统规则
                continue
            flow: Flow = self.app.flows.get(flow_id)
            if not flow:
                continue
            dpid = dp.id
            prev_bytes = flow.hop_bytes.get(dpid, 0)
            new_bytes = stat.byte_count
            prev_time = flow.hop_last_time.get(dpid, now)
            delta_t = max(1e-3, now - prev_time)
            delta_b = max(0, new_bytes - prev_bytes)
            rate_bps = int(delta_b * 8 / delta_t)

            flow.hop_bytes[dpid] = new_bytes
            flow.hop_last_time[dpid] = now
            flow.hop_rate_bps[dpid] = rate_bps

        # 这里可以调用“尾部释放逻辑”，目前先留给 scheduler_app 调用
