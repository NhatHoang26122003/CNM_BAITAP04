import sys
import os
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
from ryu.lib.packet import packet, ethernet, ipv4, tcp, udp, ether_types

# AI Imports
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'
import tensorflow as tf
from tensorflow.keras.models import load_model

# --- CẤU HÌNH ---
MODEL_DIR = "/home/nhathoang2612/CNM_Baitap04/model/"
CLS_PATH = os.path.join(MODEL_DIR, "classification")
PRED_PATH = os.path.join(MODEL_DIR, "traffic_predict")

monitor_interval = 2

# MAPPING CHUẨN TỪ QUÁ TRÌNH TRAIN (Alphabetical Order)
# ['background', 'video', 'voip', 'web']
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
        
        # Cấu hình mặc định (sẽ được cập nhật từ file config)
        self.seq_length = 10
        self.pred_type = 'LSTM'
        
        # Lưu trữ thống kê
        self.flow_stats = {}      # {dpid, flow_id}: {byte_count, packet_count, time...}
        self.path_history = {}    
        
        # Hardcode Topo: Switch 1 nối với 5 đường qua port 5,6,7,8,9
        self.uplink_ports = [5, 6, 7, 8, 9] 
        self.path_loads = {port: 0.0 for port in self.uplink_ports}
        
        # Load Models
        self.load_models()
        
        # RL Q-Table: 4 Classes x 5 Paths
        # Row 0: Background, Row 1: Video, Row 2: VoIP, Row 3: Web
        self.q_table = np.zeros((4, 5)) 
        self.epsilon = 0.1  
        self.alpha = 0.5    
        self.gamma = 0.9    

    def load_models(self):
        print(">>> [AI] Loading AI Models...")
        try:
            # 1. Classification Models
            self.cls_model = joblib.load(os.path.join(CLS_PATH, 'best_classifier_model.pkl'))
            self.cls_scaler = joblib.load(os.path.join(CLS_PATH, 'classifier_scaler.pkl'))
            print("   - Classification: Loaded DT/RF Model.")

            # 2. Prediction Models & Config
            txt_config_path = os.path.join(PRED_PATH, 'model_config.txt')
            json_config_path = os.path.join(PRED_PATH, 'model_config.json')
            
            # Ưu tiên đọc JSON, nếu không có thì đọc TXT
            if os.path.exists(json_config_path):
                with open(json_config_path, 'r') as f:
                    config = json.load(f)
                    self.pred_type = config.get('best_model_type', 'LSTM')
                    self.seq_length = int(config.get('sequence_length', 10))
                print(f"   - Config loaded from JSON (SeqLen={self.seq_length}, Type={self.pred_type})")
            
            elif os.path.exists(txt_config_path):
                with open(txt_config_path, 'r') as f:
                    content = f.read().strip()
                    self.seq_length = int(content)
                    # Nếu dùng txt, mặc định giả định là LSTM vì file .keras tồn tại
                    self.pred_type = 'LSTM' 
                print(f"   - Config loaded from TXT (SeqLen={self.seq_length}, Type={self.pred_type})")
            
            else:
                print("   ! Warning: No config file found. Using defaults (LSTM, SeqLen=10).")

            self.pred_scaler = joblib.load(os.path.join(PRED_PATH, 'prediction_scaler.pkl'))
            
            if self.pred_type == 'LSTM':
                self.pred_model = load_model(os.path.join(PRED_PATH, 'best_prediction_model.keras'))
                print("   - Prediction: Loaded LSTM Model.")
            else:
                self.pred_model = joblib.load(os.path.join(PRED_PATH, 'best_prediction_model_arima.pkl')) # Sửa lại tên file cho khớp
                print("   - Prediction: Loaded ARIMA Model.")
                
        except Exception as e:
            print(f"!!! [ERROR] Load Model Failed: {e}")
            self.cls_model = None
            self.pred_model = None

    # --- MAIN LOOP: MONITOR -> PREDICT -> OPTIMIZE ---
    def _monitor(self):
        while True:
            for dp in self.datapaths.values():
                self._request_stats(dp)
            
            # Sau khi thu thập stats, chạy AI để tối ưu định tuyến
            self._predict_traffic_load()
            hub.sleep(monitor_interval)

    def _request_stats(self, datapath):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        req = parser.OFPFlowStatsRequest(datapath)
        datapath.send_msg(req)

    # --- DỰ ĐOÁN TẢI (LSTM/ARIMA) ---
    def _predict_traffic_load(self):
        if not hasattr(self, 'pred_model') or self.pred_model is None:
            return

        # print("\n--- [AI PREDICTION] Network Status ---")
        for port in self.uplink_ports:
            # Sử dụng self.seq_length thay vì hằng số SEQ_LENGTH
            history = self.path_history.get(port, deque(maxlen=self.seq_length))
            if len(history) < self.seq_length: continue
            
            data_raw = np.array(list(history)).reshape(-1, 1)
            data_scaled = self.pred_scaler.transform(data_raw)
            # Reshape theo self.seq_length
            X_input = data_scaled.reshape(1, self.seq_length, 1)
            
            if self.pred_type == 'LSTM':
                pred_scaled = self.pred_model.predict(X_input, verbose=0)
                pred_val = self.pred_scaler.inverse_transform(pred_scaled)[0][0]
            else:
                pred_val = data_raw[-1][0] 

            self.path_loads[port] = pred_val 
            # mbps = (pred_val * 8) / 1_000_000
            # print(f"Path {port-4}: Predicted Load = {mbps:.2f} Mbps")

    # --- XỬ LÝ THỐNG KÊ & PHÂN LOẠI (CLASSIFICATION) ---
    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def _flow_stats_reply_handler(self, ev):
        body = ev.msg.body
        dpid = ev.msg.datapath.id
        
        # 1. Tổng hợp tải cho Prediction (Switch Gốc)
        if dpid == 1:
            current_port_bytes = {p: 0 for p in self.uplink_ports}
            for stat in body:
                out_port = 0
                for action in stat.instructions[0].actions if stat.instructions else []:
                    if hasattr(action, 'port'): out_port = action.port
                
                if out_port in self.uplink_ports:
                    current_port_bytes[out_port] += stat.byte_count
            
            # Update History cho LSTM
            for port in self.uplink_ports:
                if port in self.flow_stats:
                    delta = current_port_bytes[port] - self.flow_stats[port]
                    rate = max(0, delta / monitor_interval)
                else:
                    rate = 0
                self.flow_stats[port] = current_port_bytes[port]
                # Sử dụng self.seq_length
                if port not in self.path_history: self.path_history[port] = deque(maxlen=self.seq_length)
                self.path_history[port].append(rate)

        # 2. PHÂN LOẠI & REROUTING (QUAN TRỌNG: Dùng Model tại đây)
        # Chỉ xử lý các flow ứng dụng (Priority 10) tại Switch Gốc
        if dpid == 1:
            for stat in body:
                if stat.priority != 10: continue # Bỏ qua flow mặc định
                
                # Trích xuất Features cho Model Classification
                duration = stat.duration_sec
                if duration == 0: continue # Chưa đủ dữ liệu
                
                byte_count = stat.byte_count
                packet_count = stat.packet_count
                avg_packet_size = byte_count / packet_count if packet_count > 0 else 0
                byte_rate = byte_count / duration
                packet_rate = packet_count / duration
                
                # Lấy IP Proto từ match (cần parse kỹ hơn nếu muốn chính xác tuyệt đối, ở đây lấy nhanh)
                ip_proto = stat.match.get('ip_proto', 17) # Default UDP if not found
                
                # Tạo vector đặc trưng
                features = np.array([[ip_proto, packet_count, byte_count, duration, byte_rate, packet_rate, avg_packet_size]])
                
                # Scale dữ liệu
                features_scaled = self.cls_scaler.transform(features)
                
                # PREDICT (Dùng Model đã train)
                if self.cls_model:
                    pred_label_idx = self.cls_model.predict(features_scaled)[0]
                    label_name = CLASS_MAP.get(pred_label_idx, "unknown")
                    
                    # REINFORCEMENT LEARNING (Chọn đường mới dựa trên Label AI vừa đoán)
                    best_path_idx = self._get_action_rl(pred_label_idx)
                    new_out_port = self.uplink_ports[best_path_idx]
                    
                    # Kiểm tra flow hiện tại đang đi đường nào
                    current_out_port = 0
                    for action in stat.instructions[0].actions:
                        if hasattr(action, 'port'): current_out_port = action.port
                    
                    # Nếu đường mới tốt hơn đường cũ -> REROUTE (Sửa đổi Flow)
                    if current_out_port != 0 and current_out_port != new_out_port:
                        print(f"   [AI-REROUTE] Flow {label_name.upper()} (Size:{avg_packet_size:.0f}) switch from Port {current_out_port} -> {new_out_port}")
                        
                        # Update RL Reward
                        predicted_load = self.path_loads.get(new_out_port, 0)
                        reward = 1000 / (predicted_load + 1.0)
                        self._update_q_table(pred_label_idx, best_path_idx, reward)
                        
                        # Gửi lệnh FlowMod để đổi đường
                        self.mod_flow(ev.msg.datapath, stat.match, new_out_port)

    # --- PACKET IN (INITIAL SETUP) ---
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        in_port = msg.match['in_port']
        dpid = datapath.id
        
        # Học MAC
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth.ethertype == ether_types.ETH_TYPE_LLDP: return
        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid][eth.src] = in_port

        # Logic Routing Ban đầu (Fast Path)
        out_port = ofproto.OFPP_FLOOD
        
        if dpid == 1 and in_port <= 4: # Chỉ định tuyến tại Switch Gốc
            # Tạm thời Random hoặc Round-Robin để flow bắt đầu chạy
            rand_path = random.randint(0, 4)
            out_port = self.uplink_ports[rand_path]
            
            actions = [datapath.ofproto_parser.OFPActionOutput(out_port)]
            
            # Cài flow với Idle Timeout ngắn (để refresh stats)
            if eth.ethertype == ether_types.ETH_TYPE_IP:
                ip_pkt = pkt.get_protocol(ipv4.ipv4)
                match = datapath.ofproto_parser.OFPMatch(
                    in_port=in_port, eth_type=ether_types.ETH_TYPE_IP,
                    ipv4_src=ip_pkt.src, ipv4_dst=ip_pkt.dst, ip_proto=ip_pkt.proto)
                
                # Priority 10 để flow monitor bắt được
                self.add_flow(datapath, 10, match, actions, msg.buffer_id)
                return 

        # Forwarding cơ bản cho các switch khác
        if eth.dst in self.mac_to_port[dpid]:
            out_port = self.mac_to_port[dpid][eth.dst]
        
        actions = [datapath.ofproto_parser.OFPActionOutput(out_port)]
        data = None
        if msg.buffer_id == datapath.ofproto.OFP_NO_BUFFER:
            data = msg.data
        
        out = datapath.ofproto_parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                  in_port=in_port, actions=actions, data=data)
        datapath.send_msg(out)

    # --- RL FUNCTIONS ---
    def _get_action_rl(self, state):
        if random.uniform(0, 1) < self.epsilon:
            # Explore
            return random.randint(0, 4)
        else:
            return np.argmax(self.q_table[state])

    def _update_q_table(self, state, action, reward):
        old_val = self.q_table[state, action]
        next_max = np.max(self.q_table[state])
        new_val = (1 - self.alpha) * old_val + self.alpha * (reward + self.gamma * next_max)
        self.q_table[state, action] = new_val

    # --- HELPER: ADD/MOD FLOW ---
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
        """Hàm sửa đổi Flow đang chạy để đổi hướng (Reroute)"""
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        actions = [parser.OFPActionOutput(new_port)]
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(datapath=datapath, command=ofproto.OFPFC_MODIFY,
                                priority=10, match=match, instructions=inst)
        datapath.send_msg(mod)
