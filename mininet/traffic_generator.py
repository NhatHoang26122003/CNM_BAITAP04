import time
import random
import math
import sys
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from mininet.topo import Topo
from mininet.log import setLogLevel, info

# --- CẤU HÌNH HỆ THỐNG ---
CONTROLLER_IP = '127.0.0.1'
CONTROLLER_PORT = 6633
TOTAL_DURATION = 1200  # 20 phút - Đủ dài cho bài toán Prediction
POLLING_INTERVAL = 3   # Tăng nhẹ lên 3s để file CSV không bị quá nặng

# --- CẤU HÌNH MỞ RỘNG ---
NUM_PATHS = 5          
NUM_CLIENTS = 4        
NUM_SERVERS = 4        
BW_LIMIT = 20          

class ExpandedTopo(Topo):
    def build(self):
        s_src = self.addSwitch('s_src', dpid='1') 
        s_dst = self.addSwitch('s_dst', dpid='2') 
        
        link_opts = dict(bw=BW_LIMIT, delay='5ms', loss=0, max_queue_size=1000, use_htb=True)

        for i in range(1, NUM_CLIENTS + 1):
            client = self.addHost(f'h_src_{i}')
            self.addLink(client, s_src, **link_opts)
            
        for i in range(1, NUM_SERVERS + 1):
            server = self.addHost(f'h_dst_{i}')
            self.addLink(s_dst, server, **link_opts)

        for i in range(1, NUM_PATHS + 1):
            dpid_val = str(10 + i) 
            sw_path = self.addSwitch(f's_path_{i}', dpid=dpid_val)
            self.addLink(s_src, sw_path, **link_opts)
            self.addLink(sw_path, s_dst, **link_opts)

class TrafficGenerator:
    def __init__(self, net):
        self.net = net
        self.src_hosts = [net.get(f'h_src_{i}') for i in range(1, NUM_CLIENTS + 1)]
        self.dst_hosts = [net.get(f'h_dst_{i}') for i in range(1, NUM_SERVERS + 1)]

    def generate(self, duration=TOTAL_DURATION):
        info(f"*** Bắt đầu sinh traffic trong {duration}s...\n")
        
        # --- CẤU HÌNH TRỌNG SỐ TỐI ƯU CHO 1200s ---
        traffic_types = ['video', 'voip', 'web', 'background']
        # Giảm Web xuống 0.1 vì TCP sinh log gấp đôi/ba UDP
        # Giữ Background 0.1 là đủ để nhận diện nhiễu
        weights = [0.4, 0.4, 0.1, 0.1] 

        for t in range(duration):
            # In log mỗi 10 chu kỳ cho đỡ rối mắt
            if t % 10 == 0:
                info(f"--- Cycle {t}/{duration} ---\n")
                
            for src in self.src_hosts:
                if random.random() < 0.1: continue

                target = random.choice(self.dst_hosts)
                
                # Biến thiên băng thông nền tảng
                bw_pattern = abs(15 * math.sin(t / 20.0 + random.random())) + 5 
                
                traffic_type = random.choices(traffic_types, weights=weights, k=1)[0]
                
                # Burst Traffic
                is_burst = False
                if random.random() < 0.05:
                    is_burst = True
                    info(f"   [!!! BURST] {src.name} -> {target.name}\n")

                self._send_traffic(src, target, traffic_type, bw_pattern, is_burst)

            time.sleep(POLLING_INTERVAL)

    def _send_traffic(self, src, dst, traffic_type, bw_pattern, is_burst):
        target_ip = dst.IP()
        
        # --- CẤU HÌNH KÍCH THƯỚC GÓI TIN ĐA DẠNG ---
        
        if traffic_type == 'video':
            # Video: Gói to, băng thông lớn
            pkt_len = random.randint(1000, 1460)
            bw_target = bw_pattern * 1.2
            if is_burst: bw_target += 20
            cmd = f'iperf -c {target_ip} -u -b {bw_target:.2f}M -l {pkt_len} -t {POLLING_INTERVAL} -p 5001 &'
            
        elif traffic_type == 'voip':
            # VoIP: Gói nhỏ, băng thông thấp ổn định
            pkt_len = random.randint(64, 160)
            bw_target = 0.1 + random.uniform(0, 0.2)
            cmd = f'iperf -c {target_ip} -u -b {bw_target:.2f}M -l {pkt_len} -t {POLLING_INTERVAL} -p 5002 &'
            
        elif traffic_type == 'web':
            # Web: TCP (tự động MSS)
            cmd = f'iperf -c {target_ip} -t {POLLING_INTERVAL} -p 80 &'
            
        else: 
            # Background: Gói trung bình
            pkt_len = random.randint(300, 800)
            bw_target = bw_pattern * 0.5
            cmd = f'iperf -c {target_ip} -u -b {bw_target:.2f}M -l {pkt_len} -t {POLLING_INTERVAL} -p 5003 &'
        
        src.cmd(cmd)

def run():
    setLogLevel('info')
    topo = ExpandedTopo()
    c0 = RemoteController(name='c0', ip=CONTROLLER_IP, port=CONTROLLER_PORT)
    net = Mininet(topo=topo, controller=c0, link=TCLink, switch=OVSSwitch)

    try:
        net.start()
        
        info("*** Enabling STP on all switches to prevent Loops...\n")
        for s in net.switches:
            s.cmd(f'ovs-vsctl set Bridge {s.name} protocols=OpenFlow13')
            s.cmd(f'ovs-vsctl set Bridge {s.name} stp_enable=true')
        
        info("*** Waiting 40s for STP convergence...\n")
        time.sleep(40)
        
        # Bỏ PingAll
        
        info(f"*** Starting iperf servers...\n")
        dst_hosts = [net.get(f'h_dst_{i}') for i in range(1, NUM_SERVERS + 1)]
        for h in dst_hosts:
            h.cmd('iperf -s -p 5001 &') 
            h.cmd('iperf -s -p 5002 &') 
            h.cmd('iperf -s -p 5003 &') 
            h.cmd('iperf -s -p 80 &')   
            
        generator = TrafficGenerator(net)
        generator.generate()
        
    except KeyboardInterrupt:
        info("\n*** Interrupted\n")
    except Exception as e:
        info(f"\n*** Error: {e}\n")
    finally:
        info("*** Stopping network & Cleaning up\n")
        for h in net.hosts:
            h.cmd('killall iperf')
        net.stop()

if __name__ == '__main__':
    run()
