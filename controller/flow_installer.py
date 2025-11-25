# controller/flow_installer.py
from typing import Dict, Tuple
from ryu.ofproto import ofproto_v1_3
from ryu.lib import ofctl_v1_3
from ryu.base.app_manager import RyuApp
from .models import Flow


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

    def install_table0_and_table2_default(self, dp):
        """
        初始化 pipeline：
        - Table 0: DSCP 分类 + goto_table(1/2)
        - Table 2: 默认 L2 转发（这里只给个骨架，具体 L2 学习你可以结合现有代码）
        """
        ofp = dp.ofproto
        parser = dp.ofproto_parser

        # 这里简单写几个匹配 DSCP 的例子：
        # Gold: 32-47 -> metadata=1
        # Silver: 16-31 -> metadata=2
        # Best: 0-15 -> metadata=3

        def add_t0_rule(dscp_min, dscp_max, metadata_val):
            match = parser.OFPMatch(
                eth_type=0x0800,
                ip_dscp=(dscp_min, 0b111111)  # 粗略匹配，可根据需要改成范围多条规则
            )
            inst = [
                parser.OFPInstructionWriteMetadata(metadata_val, 0xffffffff),
                parser.OFPInstructionGotoTable(1)
            ]
            mod = parser.OFPFlowMod(
                datapath=dp,
                table_id=0,
                command=ofp.OFPFC_ADD,
                priority=10,
                match=match,
                instructions=inst
            )
            dp.send_msg(mod)

        # 为简化，我们只写一个“所有 IP 包 goto table 2”的默认规则
        match = parser.OFPMatch(eth_type=0x0800)
        inst = [parser.OFPInstructionGotoTable(2)]
        mod = parser.OFPFlowMod(
            datapath=dp,
            table_id=0,
            command=ofp.OFPFC_ADD,
            priority=0,
            match=match,
            instructions=inst
        )
        dp.send_msg(mod)

        # Table 2 默认直接 output=NORMAL 或交给现有 simple_switch 逻辑
        # 这里只给一个 NORMAL 动作的骨架
        actions = [parser.OFPActionOutput(ofp.OFPP_NORMAL)]
        inst2 = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        mod2 = parser.OFPFlowMod(
            datapath=dp,
            table_id=2,
            command=ofp.OFPFC_ADD,
            priority=0,
            match=parser.OFPMatch(),  # 任意
            instructions=inst2
        )
        dp.send_msg(mod2)
