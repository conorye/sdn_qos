'''
Author: yc && qq747339545@163.com
Date: 2025-11-25 09:51:25
LastEditTime: 2025-12-01 15:15:35
FilePath: /sdn_qos/controller/stats_collector.py
Description:   Stats 收集模块

Copyright (c) 2025 by ${git_name_email}, All Rights Reserved. 
'''
# controller/stats_collector.py
import threading
import time
from typing import Dict, List
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

        import os
        self.log_root = getattr(scheduler, "log_root", "/home/yc/sdn_qos/logs")
        self.fp_root = os.path.join(self.log_root, "FlowProgress")  # /home/yc/sdn_qos/logs/<run_ts>/FlowProgress
        os.makedirs(self.fp_root, exist_ok=True)
        
        # FlowProgress 打印节流：每隔多少秒记录一次
        self.flow_progress_interval = 5  # 你嫌太频繁就往上调
        self.last_flow_progress_log = 0.0

        # FlowManger 目录 & 日志文件
        self.fm_root = os.path.join(self.log_root, "FlowManger")  # 按你拼写来
        os.makedirs(self.fm_root, exist_ok=True)
        self.flow_manager_log_path = os.path.join(self.fm_root, "flow_manager.log")
        self.flow_manager_interval = 10  # FlowManager 统计频率
        self.last_flow_manager_log = 0.0
        
        
        # 新增：按空闲时间判断 finished
        self.flow_idle_timeout: float = 3.0  # 比如 3 秒无新字节就认为流结束
        self.flow_idle_since: Dict[int, float] = {}  # flow_id -> idle 起始时间
        
    def _maybe_log_flow_manager(self):
            """
            定期把当前 flows/pending_flows/active_flows 的统计写到
            /home/yc/sdn_qos/logs/<run_ts>/FlowManger/flow_manager.log
            """
            now = time.time()
            if now - self.last_flow_manager_log < self.flow_manager_interval:
                return
            self.last_flow_manager_log = now

            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

            total = len(self.s.flows)
            pending = len(self.s.pending_flows)
            active = len(self.s.active_flows)

            # 方便排查具体 flow
            pending_ids = sorted(self.s.pending_flows.keys())
            active_ids = sorted(self.s.active_flows.keys())
            finished_ids = sorted(
                fid for fid in self.s.flows.keys()
                if fid not in self.s.pending_flows and fid not in self.s.active_flows
            )

            with open(self.flow_manager_log_path, "a", encoding="utf-8") as f:
                f.write(f"{ts} [FlowManager] total={total} pending={pending} active={active} finished={len(finished_ids)}\n")
                f.write(f"{ts} [FlowManager] pending_ids={pending_ids}\n")
                f.write(f"{ts} [FlowManager] active_ids={active_ids}\n")
                f.write(f"{ts} [FlowManager] finished_ids={finished_ids}\n")

    def start(self):
        self._running = True
        self._thread.start()

    def _loop(self):
        while self._running:
            try:
                self.request_all()
                 # 新增：周期性记录 FlowManager 状态
                self._maybe_log_flow_manager()
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
            self.logger.debug(
            "[FlowStatsRaw] dpid=%s table=%d cookie=%#x pri=%d "
            "n_pkts=%d n_bytes=%d match=%s",
            dpid, st.table_id, st.cookie, st.priority,
            st.packet_count, st.byte_count, st.match
            )
            
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
            
            # ★ 新增：维护最后一跳的 idle_since
            if flow.path and dpid == flow.path[-1][0]:
                if delta_b > 0:
                    # 有新字节，说明活跃，清掉 idle 记录
                    if fid in self.flow_idle_since:
                        del self.flow_idle_since[fid]
                else:
                    # 没有新字节，如果以前没记过，就记录开始 idle 的时间
                    self.flow_idle_since.setdefault(fid, now)

        self._print_flow_progress()
        self._maybe_release()

        if now - self.last_book_dump > 3:
            self._print_port_book()
            self.last_book_dump = now

    def _log_flow_progress(self, flow: Flow, lines: List[str]):
        import os
        flow_dir = os.path.join(self.fp_root, str(flow.id))
        os.makedirs(flow_dir, exist_ok=True)
        log_path = os.path.join(flow_dir, "progress.log")

        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        with open(log_path, "a", encoding="utf-8") as f:
            for line in lines:
                f.write(f"{ts} {line}\n")


    def _print_flow_progress(self):
        now = time.time()
        # 例如每 3 秒打印一次
        if now - self.last_flow_progress_log < self.flow_progress_interval:
            return
        self.last_flow_progress_log = now

        for flow in self.s.active_flows.values():
            if not flow.path:
                continue  # 有些刚 allowed 还没填好 path，直接跳过

            last_dpid = flow.path[-1][0]
            sent = flow.hop_bytes.get(last_dpid, 0)
            rate = flow.hop_rate_bps.get(last_dpid, 0)
            total = flow.size_bytes
            rem = max(0, total - sent)
            eta = (rem * 8 / rate) if rate > 0 else -1

            hop_str = " ".join(
                f"s{dpid}={flow.hop_bytes.get(dpid,0)/1e6:.1f}MB"
                for dpid, _ in flow.path
            )

            lines = [
                f"[FlowProgress] flow={flow.id} class={flow.priority} dscp={flow.dscp}",
                f"  sent(last_hop)={sent/1e6:.2f}MB / {total/1e6:.2f}MB",
                f"  rate(last_hop)={rate/1e6:.2f}Mbps eta={eta:.1f}s",
                f"  hop_bytes: {hop_str} status={flow.status}",
            ]
            self._log_flow_progress(flow, lines)

            

    def _maybe_release(self):
        """
        尾部检测 + 逐跳释放 + 空闲超时结束：

        - 当某 hop 的 byte_count >= size_bytes * eps 时，释放前一跳规则和带宽
        - 当最后一个 hop 满足：
              * last_bytes >= size_bytes * eps  （字节数条件）
            或
              * 最后一跳持续 flow_idle_timeout 秒没有新字节（空闲条件）
          则删除整条流的规则 & 释放整条路径
        """
        # 比 1.05 放宽一点，和你看到的 1.03x 比较接近
        eps = 1.02
        now = time.time()

        for flow in list(self.s.active_flows.values()):
            # 有些极端情况 path 可能还没填好，直接跳过避免异常
            if not flow.path:
                continue

            total = flow.size_bytes * eps

            # ------- 逐跳尾部释放：byte_count >= size_bytes*eps 时，释放前一跳 -------
            for k, (dpid, port) in enumerate(flow.path):
                b = flow.hop_bytes.get(dpid, 0)
                if b >= total and dpid not in flow.released_hops and k > 0:
                    prev_dpid, prev_port = flow.path[k - 1]
                    # 1) 删除上一跳的 per-flow 规则
                    self.s.flow_installer.delete_prev_hop_flow(flow, prev_dpid)
                    # 2) 释放上一跳端口的预留带宽
                    self.s.admission.release_single_port(prev_dpid, prev_port, flow)

                    flow.released_hops.add(dpid)

                    msg = (f"[TailRelease] flow={flow.id} tail passed s{dpid}, "
                           f"release prev hop s{prev_dpid}")
                    self.logger.info(msg)
                    self._log_flow_progress(flow, [msg])

            # ------- 整条流是否结束？字节 + 空闲 两个条件择一 -------
            last_dpid = flow.path[-1][0]
            last_bytes = flow.hop_bytes.get(last_dpid, 0)

            idle_since = self.flow_idle_since.get(flow.id)
            cond_bytes = last_bytes >= total
            cond_idle = idle_since is not None and \
                        (now - idle_since >= self.flow_idle_timeout)

            # 都不满足，或者已经是 finished，就先不动
            if not (cond_bytes or cond_idle) or flow.status == "finished":
                continue

            # 标记 finished
            flow.status = "finished"
            flow.finished_at = now

            # 打一条最终 snapshot：status=finished
            last_rate = flow.hop_rate_bps.get(last_dpid, 0)
            rem = max(0, flow.size_bytes - last_bytes)
            eta = (rem * 8 / last_rate) if last_rate > 0 else -1
            hop_str = " ".join(
                f"s{dpid}={flow.hop_bytes.get(dpid, 0) / 1e6:.1f}MB"
                for dpid, _ in flow.path
            )
            final_lines = [
                f"[FlowProgress] flow={flow.id} class={flow.priority} dscp={flow.dscp}",
                f"  sent(last_hop)={last_bytes/1e6:.2f}MB / {flow.size_bytes/1e6:.2f}MB",
                f"  rate(last_hop)={last_rate/1e6:.2f}Mbps eta={eta:.1f}s",
                f"  hop_bytes: {hop_str} status={flow.status}",
            ]
            self._log_flow_progress(flow, final_lines)

            # 清掉 idle 记录，避免泄露
            if flow.id in self.flow_idle_since:
                self.flow_idle_since.pop(flow.id, None)

            # 删除全路径规则 & 释放资源
            self.s.flow_installer.delete_flow(flow)
            self.s.admission.release(flow)
            if flow.dscp is not None:
                self.s.dscp_mgr.free_dscp(flow.dscp)

            self.s.active_flows.pop(flow.id, None)

            msg = (f"[TailRelease] flow={flow.id} finished, "
                   f"released all hops & freed DSCP {flow.dscp} "
                   f"(cond_bytes={cond_bytes}, cond_idle={cond_idle})")
            self.logger.info(msg)
            self._log_flow_progress(flow, [msg])



    def _print_port_book(self):
        """
        打印当前所有端口的带宽账本到 Ryu 日志，
        同时调用 AdmissionControl.log_port_snapshot() 写快照文件。
        """
        # # 1) 打在 Ryu 日志里，方便控制台看
        # for dpid, port, cap, res, avail in self.s.admission.dump_book():
        #     self.logger.info(
        #         f"[PortBook] (s{dpid},{port}) cap={cap/1e6:.1f}M "
        #         f"reserved={res/1e6:.1f}M avail={avail/1e6:.1f}M"
        #     )

        # 2) 写全局快照日志
        try:
            self.s.admission.log_port_snapshot(tag="periodic")
        except Exception:
            self.logger.exception("log_port_snapshot failed")

    

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
