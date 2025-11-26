# controller/flow_installer.py
from typing import Dict, Tuple
from ryu.ofproto import ofproto_v1_3
from ryu.lib import ofctl_v1_3
from ryu.base.app_manager import RyuApp
from models import Flow


def make_cookie(flow_id: int, sub_id: int) -> int:
    """64bit: 高32位 flow_id，低32位 sub_id"""
    return (flow_id << 32) | (sub_id & 0xffffffff)


def flow_id_from_cookie(cookie):
    return (cookie >> 32) & 0xffffffff


class FlowInstaller:
    """
    封装 FlowMod 安装/删除逻辑。
    依赖 RyuApp 提供 datapaths 字典：dpid -> datapath
    """

    def __init__(self, app: RyuApp):
        self.app = app  # GlobalScheduler 实例，用来 access self.datapaths

    def _get_dp(self, dpid: int):
        return self.app.datapaths.get(dpid)

    def install_flow(self, flow: Flow):
        """
        在 flow.path 上每个交换机的 Table 1 安装 per-flow 规则：
        match: src_ip, dst_ip, ip_dscp
        actions: set_queue, output
        """
        ofp = ofproto_v1_3
        parser = None  # 每个 dp 自己有 parser

        for idx, (dpid, out_port) in enumerate(flow.path, start=1):
            dp = self._get_dp(dpid)
            if dp is None:
                continue
            ofp = dp.ofproto
            parser = dp.ofproto_parser

            cookie = make_cookie(flow.id, idx)

            match = parser.OFPMatch(
                eth_type=0x0800,
                ipv4_src=flow.src_ip,
                ipv4_dst=flow.dst_ip,
                ip_dscp=flow.dscp
            )

            actions = [
                parser.OFPActionSetQueue(flow.queue_id),
                parser.OFPActionOutput(out_port)
            ]
            inst = [
                parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)
            ]

            mod = parser.OFPFlowMod(
                datapath=dp,
                cookie=cookie,
                table_id=1,  # Table 1: per-flow QoS + 路由
                command=ofp.OFPFC_ADD,
                priority=200,
                match=match,
                instructions=inst,
                hard_timeout=0,
                idle_timeout=0
            )
            dp.send_msg(mod)

    def delete_flow(self, flow: Flow):
        """按 cookie 高位 flow_id 删除该流在所有 switch 的规则"""
        for dpid in {dpid for dpid, _ in flow.path}:
            self._delete_flow_in_switch(dpid, flow.id)

    def _delete_flow_in_switch(self, dpid: int, flow_id: int):
        dp = self._get_dp(dpid)
        if dp is None:
            return
        ofp = dp.ofproto
        parser = dp.ofproto_parser

        cookie = flow_id << 32
        cookie_mask = 0xffffffff00000000

        match = parser.OFPMatch()  # 匹配所有
        mod = parser.OFPFlowMod(
            datapath=dp,
            table_id=1,
            command=ofp.OFPFC_DELETE,
            cookie=cookie,
            cookie_mask=cookie_mask,
            out_port=ofp.OFPP_ANY,
            out_group=ofp.OFPG_ANY,
            match=match
        )
        dp.send_msg(mod)

    def delete_prev_hop_flow(self, flow: Flow, dpid: int):
        """只删除指定 dpid 上此 flow 的规则（逐跳释放用）"""
        self._delete_flow_in_switch(dpid, flow.id)

    def add_flow(self, datapath, table_id, priority, match, inst, cookie=0):
        ofp = datapath.ofproto
        parser = datapath.ofproto_parser
        mod = parser.OFPFlowMod(
            datapath=datapath,
            cookie=cookie,
            table_id=table_id,
            priority=priority,
            match=match,
            instructions=inst
        )
        datapath.send_msg(mod)

    
    
    def install_table0_1_2_default(self, datapath):
        """ 
        初始化 pipeline：
        - Table 0: DSCP 分类
        - Table 1: 
        """
        ofp = datapath.ofproto
        parser = datapath.ofproto_parser

        # 1) Table 0：按 DSCP 分类
        # Gold: 32-47, Silver: 16-31, Best: 0-15 (例子)
        # Gold
        match = parser.OFPMatch(eth_type=0x0800, ip_dscp=(32, 0xFC))
        inst = [
            parser.OFPInstructionWriteMetadata(1, 0xff), # class_id=1
            parser.OFPInstructionGotoTable(1)
        ]
        self.add_flow(datapath, table_id=0, priority=100, match=match, inst=inst)

        # Silver
        match = parser.OFPMatch(eth_type=0x0800, ip_dscp=(16, 0xFC))
        inst = [
            parser.OFPInstructionWriteMetadata(2, 0xff),
            parser.OFPInstructionGotoTable(1)
        ]
        self.add_flow(datapath, table_id=0, priority=90, match=match, inst=inst)

        # Best（业务但低优先）
        match = parser.OFPMatch(eth_type=0x0800, ip_dscp=(0, 0xFC))
        inst = [
            parser.OFPInstructionWriteMetadata(3, 0xff),
            parser.OFPInstructionGotoTable(1)
        ]
        self.add_flow(datapath, table_id=0, priority=80, match=match, inst=inst)

        # 其它（无 DSCP / 非 IPv4）→ 直接交给 Table 2（simple_switch 学习）
        match = parser.OFPMatch()
        inst = [parser.OFPInstructionGotoTable(2)]
        self.add_flow(datapath, table_id=0, priority=0, match=match, inst=inst)

        # 2) Table 1 默认：凡是业务 DSCP 但没专用规则的 → 也扔给 Table 2
        match = parser.OFPMatch()
        inst = [parser.OFPInstructionGotoTable(2)]
        self.add_flow(datapath, table_id=1, priority=0, match=match, inst=inst)
        
        # 3) Table 2：未匹配的流量发送给控制器（用于学习）
        match = parser.OFPMatch()
        # 关键：使用 OFPActionOutput 通过控制器学习
        inst = [
            parser.OFPInstructionActions(
                ofp.OFPIT_APPLY_ACTIONS,
                [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
            )
        ]
        self.add_flow(datapath, table_id=2, priority=0, match=match, inst=inst)
        
