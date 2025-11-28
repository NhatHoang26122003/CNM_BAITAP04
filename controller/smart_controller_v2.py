import sys
import os
import warnings

# --- TẮT CẢNH BÁO ---
os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3' 
# Tắt các cảnh báo không cần thiết
warnings.filterwarnings("ignore")
# --------------------

import time
import json
import random
import numpy as np
import pandas as pd
import joblib
from collections import deque
from operator import attrgetter

from ryu.app import simple_switch_13
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, DEAD_DISPATCHER, CONFIG_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.lib import hub
from ryu.lib.packet import packet, ethernet, ipv4, tcp, udp, ether_types

import tensorflow as tf
from tensorflow.keras.models import load_model

# CONFIG PATHS
MODEL_DIR = "/home/nhathoang2612/CNM_Baitap04/model/"
CLS_PATH = os.path.join(MODEL_DIR, "classification")
PRED_PATH = os.path.join(MODEL_DIR, "traffic_predict")

monitor_interval = 1 
CLASS_MAP = {0: 'background', 1: 'video', 2: 'voip', 3: 'web'}

# Tên các đặc trưng (Phải khớp chính xác với lúc train trong notebook)
FEATURE_NAMES = ['ip_proto', 'packet_count', 'byte_count', 
                 'duration_sec', 'byte_rate', 'packet_rate', 'avg_packet_size']

class SmartController(simple_switch_13.SimpleSwitch13):
    def __init__(self, *args, **kwargs):
        super(SmartController, self).__init__(*args, **kwargs)
        self.datapaths = {}
        self.monitor_thread = hub.spawn(self._monitor)
        
        self.seq_length = 10
        self.pred_type = 'LSTM'
        self.flow_stats = {}      
        self.path_history = {}    
        self.uplink_ports = [5, 6, 7, 8, 9] 
        self.path_loads = {port: 0.0 for port in self.uplink_ports}
        self.q_table = np.zeros((4, 5)) 
        self.epsilon = 0.1  
        self.alpha = 0.5    
        self.gamma = 0.9    
        
        self.load_models()

    def load_models(self):
        print(">>> [AI] Loading AI Models...")
        try:
            self.cls_model = joblib.load(os.path.join(CLS_PATH, 'best_classifier_model.pkl'))
            self.cls_scaler = joblib.load(os.path.join(CLS_PATH, 'classifier_scaler.pkl'))
            print("   - Classification: Loaded DT/RF Model.")

            json_config = os.path.join(PRED_PATH, 'model_config.json')
            txt_config = os.path.join(PRED_PATH, 'model_config.txt')
            
            if os.path.exists(json_config):
                with open(json_config, 'r') as f:
                    config = json.load(f)
                    self.pred_type = config.get('best_model_type', 'LSTM')
                    self.seq_length = int(config.get('sequence_length', 10))
            elif os.path.exists(txt_config):
                with open(txt_config, 'r') as f:
                    self.seq_length = int(f.read().strip())
            
            self.pred_scaler = joblib.load(os.path.join(PRED_PATH, 'prediction_scaler.pkl'))
            if self.pred_type == 'LSTM':
                self.pred_model = load_model(os.path.join(PRED_PATH, 'best_prediction_model.keras'))
            else:
                self.pred_model = joblib.load(os.path.join(PRED_PATH, 'arima_model.pkl'))
            print(f"   - Prediction: Loaded {self.pred_type} Model.")
                
        except Exception as e:
            print(f"!!! [ERROR] Load Model Failed: {e}")

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change_handler(self, ev):
        datapath = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            if datapath.id not in self.datapaths:
                print(f"   [CONNECTION] Switch {datapath.id} connected.")
                self.datapaths[datapath.id] = datapath
        elif ev.state == DEAD_DISPATCHER:
            if datapath.id in self.datapaths:
                # print(f"   [DISCONNECT] Switch {datapath.id} left.")
                del self.datapaths[datapath.id]

    def _monitor(self):
        while True:
            for dp in self.datapaths.values():
                self._request_stats(dp)
            self._predict_traffic_load()
            hub.sleep(monitor_interval)

    def _request_stats(self, datapath):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        req = parser.OFPFlowStatsRequest(datapath)
        datapath.send_msg(req)

    def _predict_traffic_load(self):
        if not hasattr(self, 'pred_model') or self.pred_model is None: return
        
        log_msg = []
        high_load = False

        for port in self.uplink_ports:
            history = self.path_history.get(port, deque(maxlen=self.seq_length))
            data_list = list(history)
            
            # Padding nếu thiếu dữ liệu
            if len(data_list) == 0: 
                val = 0
            elif len(data_list) < self.seq_length:
                padding = [data_list[-1]] * (self.seq_length - len(data_list))
                data_list = padding + data_list
                val = data_list[-1] # Fallback tạm
            
            # Dự đoán
            if len(data_list) == self.seq_length:
                data_raw = np.array(data_list).reshape(-1, 1)
                data_scaled = self.pred_scaler.transform(data_raw)
                
                if self.pred_type == 'LSTM':
                    X_input = data_scaled.reshape(1, self.seq_length, 1)
                    pred = self.pred_model.predict(X_input, verbose=0)
                    val = self.pred_scaler.inverse_transform(pred)[0][0]
                else:
                    val = data_list[-1]

            self.path_loads[port] = val
            mbps = (val * 8) / 1_000_000
            
            # Format log: P1=10.5M
            path_id = port - 4
            log_msg.append(f"P{path_id}={mbps:.1f}M")
            
            if mbps > 1.0: high_load = True

        # CHỈ IN 1 DÒNG DUY NHẤT thay vì 5 dòng
        # Và chỉ in khi tổng tải mạng có hoạt động đáng kể (>1Mbps ở bất kỳ đường nào)
        if high_load:
            print(f"   [AI PREDICT] Load Distribution: {' | '.join(log_msg)}")

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def _flow_stats_reply_handler(self, ev):
        body = ev.msg.body
        dpid = ev.msg.datapath.id
        
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
                rate = 0
                if port in self.flow_stats:
                    delta = current_port_bytes[port] - self.flow_stats[port]
                    rate = max(0, delta / monitor_interval)
                self.flow_stats[port] = current_port_bytes[port]
                if port not in self.path_history: self.path_history[port] = deque(maxlen=self.seq_length)
                self.path_history[port].append(rate)

        # 2. AI CLASSIFICATION & REROUTING
        if dpid == 1:
            for stat in body:
                if stat.priority != 10: continue
                if stat.duration_sec == 0: continue
                
                byte_count = stat.byte_count
                packet_count = stat.packet_count
                avg_packet_size = byte_count / packet_count if packet_count > 0 else 0
                byte_rate = byte_count / stat.duration_sec
                packet_rate = packet_count / stat.duration_sec
                ip_proto = stat.match.get('ip_proto', 17)
                
                # --- FIX: Dùng DataFrame để có tên cột, tránh warning ---
                features_df = pd.DataFrame([[ip_proto, packet_count, byte_count, 
                                             stat.duration_sec, byte_rate, packet_rate, avg_packet_size]], 
                                           columns=FEATURE_NAMES)
                
                if self.cls_model:
                    # --- AI CLASSIFIER ---
                    # Scale bằng DataFrame đã có tên cột
                    features_scaled = self.cls_scaler.transform(features_df)
                    pred_idx = self.cls_model.predict(features_scaled)[0]
                    label = CLASS_MAP.get(pred_idx, "unknown")
                    
                    # Log phân loại (In tất cả các loại quan trọng)
                    if label in ['video', 'voip', 'web'] or byte_rate > 100000:
                        print(f"   [AI CLASSIFIER] Flow {label.upper()} detected (Size: {avg_packet_size:.0f} bytes)")

                    # RL Chọn đường
                    best_path_idx = self._get_action_rl(pred_idx)
                    new_out_port = self.uplink_ports[best_path_idx]
                    
                    curr_port = 0
                    if stat.instructions:
                        for action in stat.instructions[0].actions:
                            if hasattr(action, 'port'): curr_port = action.port
                    
                    if curr_port != 0 and curr_port != new_out_port:
                        print(f"   >>> [AI-REROUTE] Optimizing {label.upper()}: Switch to Path {new_out_port-4}")
                        
                        reward = 1000 / (self.path_loads.get(new_out_port, 0) + 1.0)
                        self._update_q_table(pred_idx, best_path_idx, reward)
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
        
        if eth.ethertype in [ether_types.ETH_TYPE_LLDP, 34525, ether_types.ETH_TYPE_ARP]: return

        if eth.ethertype == ether_types.ETH_TYPE_IP:
            self.mac_to_port.setdefault(dpid, {})
            self.mac_to_port[dpid][eth.src] = in_port
            
            if dpid == 1 and in_port <= 4:
                ip = pkt.get_protocol(ipv4.ipv4)
                print(f"[NEW FLOW] {ip.src} -> {ip.dst}")
                
                rand_path = random.randint(0, 4)
                out_port = self.uplink_ports[rand_path]
                
                actions = [parser.OFPActionOutput(out_port)]
                match = parser.OFPMatch(
                    in_port=in_port, eth_type=ether_types.ETH_TYPE_IP,
                    ipv4_src=ip.src, ipv4_dst=ip.dst, ip_proto=ip.proto)
                
                self.add_flow(datapath, 10, match, actions, msg.buffer_id, idle_timeout=5)
                return

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

    def _get_action_rl(self, state):
        if random.uniform(0, 1) < self.epsilon: return random.randint(0, 4)
        return np.argmax(self.q_table[state])

    def _update_q_table(self, state, action, reward):
        old = self.q_table[state, action]
        mx = np.max(self.q_table[state])
        self.q_table[state, action] = (1 - self.alpha) * old + self.alpha * (reward + self.gamma * mx)

    def add_flow(self, datapath, priority, match, actions, buffer_id=None, idle_timeout=0):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        if buffer_id:
            mod = parser.OFPFlowMod(datapath=datapath, buffer_id=buffer_id, priority=priority, match=match, idle_timeout=idle_timeout, instructions=inst)
        else:
            mod = parser.OFPFlowMod(datapath=datapath, priority=priority, match=match, idle_timeout=idle_timeout, instructions=inst)
        datapath.send_msg(mod)

    def mod_flow(self, datapath, match, new_port):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        actions = [parser.OFPActionOutput(new_port)]
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(datapath=datapath, command=ofproto.OFPFC_MODIFY, priority=10, match=match, instructions=inst)
        datapath.send_msg(mod)
