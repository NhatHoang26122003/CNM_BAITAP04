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
        
        self.file_name = "network_traffic_data.csv"
        
        # Mở file với chế độ 'a' (append), buffering=1 để ghi ngay lập tức
        self.csv_file = open(self.file_name, 'a', newline='')
        self.writer = csv.writer(self.csv_file)
        
        # Kiểm tra file tồn tại để ghi header
        file_exists = os.path.isfile(self.file_name) and os.path.getsize(self.file_name) > 0
        
        if not file_exists:
            self.writer.writerow([
                'timestamp', 'datapath_id', 'flow_id', 
                'ip_src', 'ip_dst', 'ip_proto', 'tp_src', 'tp_dst', # Thêm tp_src/dst (Port)
                'packet_count', 'byte_count', 
                'duration_sec', 'duration_nsec',
                'byte_rate', 'packet_rate', 
                'label'
            ])
            self.csv_file.flush()
            
        self.previous_stats = {} 
        print(f"TrafficCollector started. Logging to {self.file_name}")

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change_handler(self, ev):
        datapath = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            if datapath.id not in self.datapaths:
                print(f"Datapath {datapath.id} connected.")
                self.datapaths[datapath.id] = datapath
        elif ev.state == DEAD_DISPATCHER:
            if datapath.id in self.datapaths:
                print(f"Datapath {datapath.id} disconnected.")
                del self.datapaths[datapath.id]

    # --- QUAN TRỌNG: Cài đặt Table-Miss Flow (Mặc định gửi về Controller) ---
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # Cài đặt Table-miss flow entry (Priority 0)
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions)
        print(f"Installed Table-Miss flow for Datapath {datapath.id}")

    def _monitor(self):
        while True:
            for dp in self.datapaths.values():
                self._request_stats(dp)
            hub.sleep(2) # Polling interval

    def _request_stats(self, datapath):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        req = parser.OFPFlowStatsRequest(datapath)
        datapath.send_msg(req)

    # --- XỬ LÝ PACKET_IN: Cài đặt Flow chi tiết theo PORT ---
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet) # Sửa get_protocols thành get_protocol cho an toàn

        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        dst = eth.dst
        src = eth.src
        dpid = datapath.id
        self.mac_to_port.setdefault(dpid, {})

        # Learn MAC address
        self.mac_to_port[dpid][src] = in_port

        out_port = ofproto.OFPP_FLOOD
        if dst in self.mac_to_port[dpid]:
            out_port = self.mac_to_port[dpid][dst]

        actions = [parser.OFPActionOutput(out_port)]

        # Nếu đã biết cổng ra, cài đặt Flow Rule
        if out_port != ofproto.OFPP_FLOOD:
            
            # Kiểm tra xem có phải gói tin IP không
            if eth.ethertype == ether_types.ETH_TYPE_IP:
                ip_pkt = pkt.get_protocol(ipv4.ipv4)
                
                # Mặc định match IP cơ bản
                match_args = {
                    'in_port': in_port,
                    'eth_dst': dst,
                    'eth_type': ether_types.ETH_TYPE_IP,
                    'ipv4_src': ip_pkt.src,
                    'ipv4_dst': ip_pkt.dst,
                    'ip_proto': ip_pkt.proto
                }
                
                # Xử lý chi tiết TCP/UDP để lấy Port
                if ip_pkt.proto == 6: # TCP
                    tcp_pkt = pkt.get_protocol(tcp.tcp)
                    if tcp_pkt: # Kiểm tra tồn tại
                        match_args['tcp_src'] = tcp_pkt.src_port
                        match_args['tcp_dst'] = tcp_pkt.dst_port
                elif ip_pkt.proto == 17: # UDP
                    udp_pkt = pkt.get_protocol(udp.udp)
                    if udp_pkt: # Kiểm tra tồn tại
                        match_args['udp_src'] = udp_pkt.src_port
                        match_args['udp_dst'] = udp_pkt.dst_port
                
                # Tạo Match object từ dictionary
                match = parser.OFPMatch(**match_args)
                
                # Cài đặt flow với Priority cao (10) để ưu tiên xử lý IP/Port
                # Thêm idle_timeout để flow tự hủy khi không có traffic (tránh rác)
                print(f"Installing Flow: {ip_pkt.src} -> {ip_pkt.dst} (Proto: {ip_pkt.proto})") # Debug log
                self.add_flow(datapath, 10, match, actions, msg.buffer_id, idle_timeout=20, hard_timeout=0)
                return 

            else:
                # Các gói tin non-IP (ARP, etc.)
                match = parser.OFPMatch(in_port=in_port, eth_dst=dst)
                self.add_flow(datapath, 1, match, actions, msg.buffer_id)
        
        # PacketOut nếu cần flood hoặc gửi gói đầu tiên
        data = None
        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            data = msg.data
        out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                  in_port=in_port, actions=actions, data=data)
        datapath.send_msg(out)
        
    def add_flow(self, datapath, priority, match, actions, buffer_id=None, idle_timeout=0, hard_timeout=0):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS,
                                             actions)]
        if buffer_id:
            mod = parser.OFPFlowMod(datapath=datapath, buffer_id=buffer_id,
                                    priority=priority, match=match,
                                    idle_timeout=idle_timeout, 
                                    hard_timeout=hard_timeout,
                                    instructions=inst)
        else:
            mod = parser.OFPFlowMod(datapath=datapath, priority=priority,
                                    match=match, 
                                    idle_timeout=idle_timeout,
                                    hard_timeout=hard_timeout,
                                    instructions=inst)
        datapath.send_msg(mod)

    # --- XỬ LÝ STATS: Thu thập, tính toán và Gán nhãn ---
    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def _flow_stats_reply_handler(self, ev):
        body = ev.msg.body
        timestamp = time.time()
        
        # Lọc các flow IP (priority 10 đã set ở trên)
        sorted_flows = sorted([flow for flow in body if flow.priority == 10],
                              key=lambda flow: (flow.match.get('in_port', 0), flow.match.get('eth_dst', '')))

        for stat in sorted_flows:
            # Lấy thông tin cơ bản
            ip_src = stat.match.get('ipv4_src', '0.0.0.0')
            ip_dst = stat.match.get('ipv4_dst', '0.0.0.0')
            ip_proto = stat.match.get('ip_proto', 0)
            
            # Lấy thông tin Port (Layer 4) để phân loại
            tp_src = 0
            tp_dst = 0
            
            if ip_proto == 6: # TCP
                tp_src = stat.match.get('tcp_src', 0)
                tp_dst = stat.match.get('tcp_dst', 0)
            elif ip_proto == 17: # UDP
                tp_src = stat.match.get('udp_src', 0)
                tp_dst = stat.match.get('udp_dst', 0)
            
            # Tạo Key duy nhất cho Flow này
            flow_key = (ev.msg.datapath.id, ip_src, ip_dst, ip_proto, tp_src, tp_dst)
            
            # Tính toán tốc độ (Rate)
            byte_rate = 0.0
            packet_rate = 0.0
            
            if flow_key in self.previous_stats:
                prev_stat = self.previous_stats[flow_key]
                delta_bytes = stat.byte_count - prev_stat['byte_count']
                delta_packets = stat.packet_count - prev_stat['packet_count']
                delta_sec = stat.duration_sec - prev_stat['duration_sec']
                delta_nsec = stat.duration_nsec - prev_stat['duration_nsec']
                
                total_delta_time = delta_sec + (delta_nsec / 1000000000.0)

                if total_delta_time > 0.01: # Tránh chia cho số quá nhỏ
                    byte_rate = delta_bytes / total_delta_time
                    packet_rate = delta_packets / total_delta_time
            
            # Lưu trạng thái hiện tại
            self.previous_stats[flow_key] = {
                'byte_count': stat.byte_count,
                'packet_count': stat.packet_count,
                'duration_sec': stat.duration_sec,
                'duration_nsec': stat.duration_nsec
            }

            # --- LOGIC GÁN NHÃN (LABELING) ---
            # Logic này phải khớp với kịch bản Mininet (Port của iperf server)
            label = 'unknown'
            if tp_dst == 5001: 
                label = 'video'
            elif tp_dst == 5002: 
                label = 'voip'
            elif tp_dst == 80 or tp_src == 80: 
                label = 'web'
            else:
                label = 'background' # Các lưu lượng khác (ARP, ICMP, Noise...)

            # Chỉ ghi file nếu có dữ liệu truyền qua (byte_rate > 0 hoặc mới xuất hiện)
            # Giúp file CSV gọn hơn, tránh ghi dòng toàn số 0
            if stat.byte_count > 0:
                self.writer.writerow([
                    timestamp, ev.msg.datapath.id, flow_key, 
                    ip_src, ip_dst, ip_proto, tp_src, tp_dst,
                    stat.packet_count, stat.byte_count, 
                    stat.duration_sec, stat.duration_nsec,
                    byte_rate, packet_rate, 
                    label
                ])
        
        self.csv_file.flush()
