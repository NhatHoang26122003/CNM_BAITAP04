import sys
import os

# --- TẮT CẢNH BÁO GPU/TENSORFLOW ---
os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3' 
# -----------------------------------

import time
import json
import random
import numpy as np
import pandas as pd
import joblib
from collections import deque
from operator import attrgetter

# Ryu Imports
from ryu.app import simple_switch_13
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, DEAD_DISPATCHER, CONFIG_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.lib import hub
from ryu.lib.packet import packet, ethernet, ipv4, tcp, udp, arp, ether_types

# AI Imports
import tensorflow as tf
from tensorflow.keras.models import load_model

# --- CẤU HÌNH ---
MODEL_DIR = "/home/nhathoang2612/CNM_Baitap04/model/"
CLS_PATH = os.path.join(MODEL_DIR, "classification")
PRED_PATH = os.path.join(MODEL_DIR, "traffic_predict")

monitor_interval = 2 # Chu kỳ in log

# MAPPING CHUẨN
CLASS_MAP = {
    0: 'background',
    1: 'video',
    2: 'voip',
    3: 'web'
}

class SmartController(simple_switch_13.SimpleSwitch13):
    def __init__(self, *args, **kwargs):
        super(SmartController, self).__init__(*args, **kwargs)
        self.datapaths = {}
        self.monitor_thread = hub.spawn(self._monitor)
        
        self.seq_length = 10
        self.pred_type = 'LSTM'
        
        self.flow_stats = {}      
        self.path_history = {}    
        
        # Hardcode Topo: Switch 1 nối với 5 đường qua port 5,6,7,8,9
        self.uplink_ports = [5, 6, 7, 8, 9] 
        self.path_loads = {port: 0.0 for port in self.uplink_ports}
        
        self.load_models()
        
        # RL Q-Table
        self.q_table = np.zeros((4, 5)) 
        self.epsilon = 0.1  
        self.alpha = 0.5    
        self.gamma = 0.9    

    def load_models(self):
        print(">>> [AI] Loading AI Models...")
        try:
            # Classification
            self.cls_model = joblib.load(os.path.join(CLS_PATH, 'best_classifier_model.pkl'))
            self.cls_scaler = joblib.load(os.path.join(CLS_PATH, 'classifier_scaler.pkl'))
            print("   - Classification: Loaded DT/RF Model.")

            # Prediction & Config
            txt_config = os.path.join(PRED_PATH, 'model_config.txt')
            json_config = os.path.join(PRED_PATH, 'model_config.json')
            
            if os.path.exists(json_config):
                with open(json_config, 'r') as f:
                    config = json.load(f)
                    self.pred_type = config.get('best_model_type', 'LSTM')
                    self.seq_length = int(config.get('sequence_length', 10))
            elif os.path.exists(txt_config):
                with open(txt_config, 'r') as f:
                    self.seq_length = int(f.read().strip())
                    self.pred_type = 'LSTM' 
            
            self.pred_scaler = joblib.load(os.path.join(PRED_PATH, 'prediction_scaler.pkl'))
            
            if self.pred_type == 'LSTM':
                self.pred_model = load_model(os.path.join(PRED_PATH, 'best_prediction_model.keras'))
            else:
                self.pred_model = joblib.load(os.path.join(PRED_PATH, 'arima_model.pkl'))
            print(f"   - Prediction: Loaded {self.pred_type} Model.")
                
        except Exception as e:
            print(f"!!! [ERROR] Load Model Failed: {e}")
            self.cls_model = None
            self.pred_model = None

    def _monitor(self):
        while True:
            for dp in self.datapaths.values():
                self._request_stats(dp)
            hub.sleep(monitor_interval)

    def _request_stats(self, datapath):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        req = parser.OFPFlowStatsRequest(datapath)
        datapath.send_msg(req)

    def _predict_traffic_load(self):
        """Dự đoán tải và IN LOG trạng thái mạng"""
        if not hasattr(self, 'pred_model') or self.pred_model is None: return

        # print("\n--- [AI MONITOR] Network Status ---")
        for port in self.uplink_ports:
            history = self.path_history.get(port, deque(maxlen=self.seq_length))
            if len(history) < self.seq_length: continue
            
            data_raw = np.array(list(history)).reshape(-1, 1)
            data_scaled = self.pred_scaler.transform(data_raw)
            X_input = data_scaled.reshape(1, self.seq_length, 1)
            
            if self.pred_type == 'LSTM':
                pred_scaled = self.pred_model.predict(X_input, verbose=0)
                pred_val = self.pred_scaler.inverse_transform(pred_scaled)[0][0]
            else:
                pred_val = data_raw[-1][0] 

            self.path_loads[port] = pred_val 
            
            # IN LOG NẾU CÓ TẢI CAO (Để demo thấy AI hoạt động)
            mbps = (pred_val * 8) / 1_000_000
            if mbps > 1.0: # Chỉ in nếu > 1Mbps để đỡ rối
                print(f"   [LSTM PREDICT] Port {port}: Predicted Load = {mbps:.2f} Mbps")

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def _flow_stats_reply_handler(self, ev):
        body = ev.msg.body
        dpid = ev.msg.datapath.id
        
        # 1. Thu thập dữ liệu
        if dpid == 1:
            current_port_bytes = {p: 0 for p in self.uplink_ports}
            for stat in body:
                out_port = 0
                if stat.instructions:
                    for action in stat.instructions[0].actions:
                        if hasattr(action, 'port'): out_port = action.port
                if out_port in self.uplink_ports:
                    current_port_bytes[out_port] += stat.byte_count
            
            for port in self.uplink_ports:
                if port in self.flow_stats:
                    delta = current_port_bytes[port] - self.flow_stats[port]
                    rate = max(0, delta / monitor_interval)
                else:
                    rate = 0
                self.flow_stats[port] = current_port_bytes[port]
                if port not in self.path_history: self.path_history[port] = deque(maxlen=self.seq_length)
                self.path_history[port].append(rate)
            
            # Gọi dự đoán sau khi cập nhật dữ liệu
            self._predict_traffic_load()

        # 2. AI CLASSIFICATION & REROUTING
        if dpid == 1:
            for stat in body:
                if stat.priority != 10: continue 
                
                duration = stat.duration_sec
                if duration == 0: continue
                
                byte_count = stat.byte_count
                packet_count = stat.packet_count
                avg_packet_size = byte_count / packet_count if packet_count > 0 else 0
                byte_rate = byte_count / duration
                packet_rate = packet_count / duration
                ip_proto = stat.match.get('ip_proto', 17)
                
                features = np.array([[ip_proto, packet_count, byte_count, duration, byte_rate, packet_rate, avg_packet_size]])
                
                if self.cls_model:
                    # AI Phân loại
                    features_scaled = self.cls_scaler.transform(features)
                    pred_label_idx = self.cls_model.predict(features_scaled)[0]
                    label_name = CLASS_MAP.get(pred_label_idx, "unknown")
                    
                    # RL Chọn đường
                    best_path_idx = self._get_action_rl(pred_label_idx)
                    new_out_port = self.uplink_ports[best_path_idx]
                    
                    # Kiểm tra đường hiện tại
                    current_out_port = 0
                    if stat.instructions:
                        for action in stat.instructions[0].actions:
                            if hasattr(action, 'port'): current_out_port = action.port
                    
                    # IN LOG TRẠNG THÁI (Để bạn thấy nó hoạt động)
                    # print(f"   [MONITOR] Flow {label_name.upper()} | Size: {avg_packet_size:.0f} | Current Port: {current_out_port}")

                    # Nếu cần đổi đường
                    if current_out_port != 0 and current_out_port != new_out_port:
                        print(f"   >>> [AI-REROUTE] Flow {label_name.upper()} switched: Port {current_out_port} -> {new_out_port} (Optimized)")
                        
                        predicted_load = self.path_loads.get(new_out_port, 0)
                        reward = 1000 / (predicted_load + 1.0)
                        self._update_q_table(pred_label_idx, best_path_idx, reward)
                        
                        self.mod_flow(ev.msg.datapath, stat.match, new_out_port)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match['in_port']
        dpid = datapath.id
        
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        
        # Bỏ qua LLDP
        if eth.ethertype == ether_types.ETH_TYPE_LLDP: return

        # --- [FIX QUAN TRỌNG] XỬ LÝ ARP CHỐNG LOOP ---
        if eth.ethertype == ether_types.ETH_TYPE_ARP:
            # Nếu là Switch Gốc (1) hoặc Đích (2)
            if dpid in [1, 2]:
                # Chỉ Flood ra các cổng Host (1-4)
                actions = [parser.OFPActionOutput(p) for p in range(1, 5) if p != in_port]
                
                # VÀ CHỈ GỬI QUA ĐƯỜNG SỐ 1 (Port 5) ĐỂ SANG BÊN KIA
                # (Chặn không cho ARP đi qua Port 6,7,8,9 để tránh Loop)
                
                # Nếu gói tin từ Host gửi lên -> Cho đi sang Port 5
                if in_port <= 4:
                    actions.append(parser.OFPActionOutput(5))
                
                # Nếu gói tin từ Port 5 về -> Chỉ flood ra Host (đã làm ở trên), ko gửi lại Uplink
                
                out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                          in_port=in_port, actions=actions, data=msg.data)
                datapath.send_msg(out)
                return
            else:
                # Các switch trung gian (s_path): Flood bình thường (vì nó thẳng hàng, ko loop)
                actions = [parser.OFPActionOutput(ofproto.OFPP_FLOOD)]
                out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                          in_port=in_port, actions=actions, data=msg.data)
                datapath.send_msg(out)
                return

        # --- XỬ LÝ IP (DATA TRAFFIC - AI ROUTING) ---
        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid][eth.src] = in_port

        # Logic Routing tại Switch Gốc (dpid=1)
        if dpid == 1 and in_port <= 4: 
            # In Log để biết có Flow mới
            if eth.ethertype == ether_types.ETH_TYPE_IP:
                ip = pkt.get_protocol(ipv4.ipv4)
                print(f"[NEW FLOW] {ip.src} -> {ip.dst} | Initializing Fast Path...")

            # Fast Path: Gán tạm một đường (Random) để traffic bắt đầu chạy
            # Sau 2s, AI Monitor sẽ bắt được stats và tối ưu lại (Reroute)
            rand_path = random.randint(0, 4)
            out_port = self.uplink_ports[rand_path]
            actions = [parser.OFPActionOutput(out_port)]
            
            if eth.ethertype == ether_types.ETH_TYPE_IP:
                ip_pkt = pkt.get_protocol(ipv4.ipv4)
                match = parser.OFPMatch(
                    in_port=in_port, eth_type=ether_types.ETH_TYPE_IP,
                    ipv4_src=ip_pkt.src, ipv4_dst=ip_pkt.dst, ip_proto=ip_pkt.proto)
                
                # Priority 10, Idle Timeout 5s (để refresh liên tục)
                self.add_flow(datapath, 10, match, actions, msg.buffer_id, idle_timeout=5)
                return 

        # Forwarding cơ bản tại các Switch khác
        if eth.dst in self.mac_to_port[dpid]:
            out_port = self.mac_to_port[dpid][eth.dst]
        else:
            out_port = ofproto.OFPP_FLOOD
        
        actions = [parser.OFPActionOutput(out_port)]
        data = None
        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            data = msg.data
        
        out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                  in_port=in_port, actions=actions, data=data)
        datapath.send_msg(out)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        
        print(f"   [CONNECTION] Switch {datapath.id} connected.")

        # Table-miss flow (Gửi về Controller nếu không khớp rule nào)
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions)
        
    def _get_action_rl(self, state):
        if random.uniform(0, 1) < self.epsilon:
            return random.randint(0, 4)
        else:
            return np.argmax(self.q_table[state])

    def _update_q_table(self, state, action, reward):
        old_val = self.q_table[state, action]
        next_max = np.max(self.q_table[state])
        new_val = (1 - self.alpha) * old_val + self.alpha * (reward + self.gamma * next_max)
        self.q_table[state, action] = new_val

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

    def mod_flow(self, datapath, match, new_port):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        actions = [parser.OFPActionOutput(new_port)]
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(datapath=datapath, command=ofproto.OFPFC_MODIFY,
                                priority=10, match=match, instructions=inst)
        datapath.send_msg(mod)
