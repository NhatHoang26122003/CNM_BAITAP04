import csv
import os
import time
from operator import attrgetter
from ryu.app import simple_switch_13
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, DEAD_DISPATCHER, CONFIG_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.lib import hub
from ryu.lib.packet import packet, ethernet, ipv4, tcp, udp, ether_types

class TrafficCollector(simple_switch_13.SimpleSwitch13):
    def __init__(self, *args, **kwargs):
        super(TrafficCollector, self).__init__(*args, **kwargs)
        self.datapaths = {}
        self.monitor_thread = hub.spawn(self._monitor)
        
        # Lấy đường dẫn tuyệt đối để chắc chắn biết file nằm đâu
        self.file_name = "network_traffic_data.csv"
        self.full_path = os.path.abspath(self.file_name)
        
        print(f"--- LOGGING DATA TO: {self.full_path} ---")

        # Chế độ 'a' (append) nhưng buffering=1 (line buffering) để ghi ngay
        self.csv_file = open(self.file_name, 'a', newline='', buffering=1)
        self.writer = csv.writer(self.csv_file)
        
        # Ghi Header nếu file mới
        if os.path.getsize(self.file_name) == 0:
            self.writer.writerow([
                'timestamp', 'datapath_id', 'flow_id', 
                'ip_src', 'ip_dst', 'ip_proto', 
                'src_port', 'dst_port',
                'packet_count', 'byte_count', 
                'duration_sec', 'duration_nsec',
                'byte_rate', 'packet_rate', 
                'label'
            ])
            self.csv_file.flush()
            
        self.previous_stats = {} 
        print(f"TrafficCollector started.")

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change_handler(self, ev):
        datapath = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            if datapath.id not in self.datapaths:
                print(f"Datapath {datapath.id} connected.")
                self.datapaths[datapath.id] = datapath
        elif ev.state == DEAD_DISPATCHER:
            if datapath.id in self.datapaths:
                del self.datapaths[datapath.id]

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        
        # Table-miss flow
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions)

    def _monitor(self):
        while True:
            for dp in self.datapaths.values():
                self._request_stats(dp)
            hub.sleep(2) 

    def _request_stats(self, datapath):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        req = parser.OFPFlowStatsRequest(datapath)
        datapath.send_msg(req)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        
        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        dst = eth.dst
        src = eth.src
        dpid = datapath.id
        self.mac_to_port.setdefault(dpid, {})

        # Learn MAC
        self.mac_to_port[dpid][src] = in_port

        out_port = ofproto.OFPP_FLOOD
        if dst in self.mac_to_port[dpid]:
            out_port = self.mac_to_port[dpid][dst]
        
        actions = [parser.OFPActionOutput(out_port)]

        # CHỈ CÀI FLOW KHI ĐÃ BIẾT CỔNG RA (UNICAST)
        if out_port != ofproto.OFPP_FLOOD:
            if eth.ethertype == ether_types.ETH_TYPE_IP:
                ip_pkt = pkt.get_protocol(ipv4.ipv4)
                
                match_args = {
                    'in_port': in_port,
                    'eth_dst': dst,
                    'eth_type': ether_types.ETH_TYPE_IP,
                    'ipv4_src': ip_pkt.src,
                    'ipv4_dst': ip_pkt.dst,
                    'ip_proto': ip_pkt.proto
                }
                
                if ip_pkt.proto == 6: # TCP
                    tcp_pkt = pkt.get_protocol(tcp.tcp)
                    match_args['tcp_src'] = tcp_pkt.src_port
                    match_args['tcp_dst'] = tcp_pkt.dst_port
                elif ip_pkt.proto == 17: # UDP
                    udp_pkt = pkt.get_protocol(udp.udp)
                    match_args['udp_src'] = udp_pkt.src_port
                    match_args['udp_dst'] = udp_pkt.dst_port
                
                match = parser.OFPMatch(**match_args)
                
                # Priority 10 để filter khi lấy stats
                self.add_flow(datapath, 10, match, actions, msg.buffer_id, idle_timeout=10)
                return

        data = None
        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            data = msg.data
        out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                  in_port=in_port, actions=actions, data=data)
        datapath.send_msg(out)

    def add_flow(self, datapath, priority, match, actions, buffer_id=None, idle_timeout=0):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        
        if buffer_id:
            mod = parser.OFPFlowMod(datapath=datapath, buffer_id=buffer_id,
                                    priority=priority, match=match,
                                    idle_timeout=idle_timeout, instructions=inst)
        else:
            mod = parser.OFPFlowMod(datapath=datapath, priority=priority,
                                    match=match, idle_timeout=idle_timeout, 
                                    instructions=inst)
        datapath.send_msg(mod)

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def _flow_stats_reply_handler(self, ev):
        body = ev.msg.body
        timestamp = time.time()
        
        # Chỉ lấy Priority 10 (IP Traffic)
        target_flows = [flow for flow in body if flow.priority == 10]
        
        row_count = 0
        for stat in target_flows:
            # Parse IP/Port
            ip_src = stat.match.get('ipv4_src', '0.0.0.0')
            ip_dst = stat.match.get('ipv4_dst', '0.0.0.0')
            ip_proto = stat.match.get('ip_proto', 0)
            
            src_port = 0
            dst_port = 0
            if ip_proto == 6:
                src_port = stat.match.get('tcp_src', 0)
                dst_port = stat.match.get('tcp_dst', 0)
            elif ip_proto == 17:
                src_port = stat.match.get('udp_src', 0)
                dst_port = stat.match.get('udp_dst', 0)
            
            flow_key = (ev.msg.datapath.id, ip_src, ip_dst, ip_proto, src_port, dst_port)
            
            byte_rate = 0.0
            packet_rate = 0.0
            
            if flow_key in self.previous_stats:
                prev = self.previous_stats[flow_key]
                d_bytes = stat.byte_count - prev['byte_count']
                d_pkts = stat.packet_count - prev['packet_count']
                d_time = (stat.duration_sec + stat.duration_nsec/1e9) - (prev['duration_sec'] + prev['duration_nsec']/1e9)
                
                # Tránh chia cho 0 hoặc số quá nhỏ
                if d_time > 0.1:
                    byte_rate = d_bytes / d_time
                    packet_rate = d_pkts / d_time
            
            self.previous_stats[flow_key] = {
                'byte_count': stat.byte_count,
                'packet_count': stat.packet_count,
                'duration_sec': stat.duration_sec,
                'duration_nsec': stat.duration_nsec
            }

            # Labeling
            label = 'unknown'
            if dst_port == 5001: label = 'video'
            elif dst_port == 5002: label = 'voip'
            elif dst_port == 80 or src_port == 80: label = 'web'
            elif dst_port == 5003: label = 'background'
            else: label = 'control_traffic'

            # Chỉ ghi nếu có dữ liệu thực tế
            if stat.byte_count > 0:
                self.writer.writerow([
                    timestamp, ev.msg.datapath.id, flow_key, 
                    ip_src, ip_dst, ip_proto, src_port, dst_port,
                    stat.packet_count, stat.byte_count, 
                    stat.duration_sec, stat.duration_nsec,
                    byte_rate, packet_rate, 
                    label
                ])
                row_count += 1
        
        # In log ra terminal để bạn biết là đang có ghi file
        if row_count > 0:
            print(f"Logged {row_count} rows from Switch {ev.msg.datapath.id}")
