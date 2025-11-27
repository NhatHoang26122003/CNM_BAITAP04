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
TOTAL_DURATION = 100   # Tăng thời gian lên để kịp thu thập data
POLLING_INTERVAL = 2   

# --- CẤU HÌNH MỞ RỘNG ---
NUM_PATHS = 5          
NUM_CLIENTS = 4        
NUM_SERVERS = 4        
BW_LIMIT = 20          

class ExpandedTopo(Topo):
    def build(self):
        # 1. Tạo 2 Switch gom
        s_src = self.addSwitch('s_src', dpid='1') 
        s_dst = self.addSwitch('s_dst', dpid='2') 
        
        link_opts = dict(bw=BW_LIMIT, delay='5ms', loss=0, max_queue_size=1000, use_htb=True)

        # 2. Tạo Hosts
        for i in range(1, NUM_CLIENTS + 1):
            client = self.addHost(f'h_src_{i}')
            self.addLink(client, s_src, **link_opts)
            
        for i in range(1, NUM_SERVERS + 1):
            server = self.addHost(f'h_dst_{i}')
            self.addLink(s_dst, server, **link_opts)

        # 3. Tạo các đường đi song song
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
        
        for t in range(duration):
            info(f"--- Cycle {t}/{duration} ---\n")
            for src in self.src_hosts:
                if random.random() < 0.3: continue

                target = random.choice(self.dst_hosts)
                
                # Biến thiên băng thông
                bw_pattern = abs(15 * math.sin(t / 10.0 + random.random())) + 5 
                noise = random.uniform(-3, 3)
                current_bw = max(1, bw_pattern + noise)

                traffic_type = random.choice(['video', 'voip', 'web', 'background'])
                
                if random.random() < 0.05:
                    current_bw += 25
                    info(f"   [!!! BURST] {src.name} -> {target.name}\n")

                self._send_traffic(src, target, traffic_type, current_bw)

            time.sleep(POLLING_INTERVAL)

    def _send_traffic(self, src, dst, traffic_type, bandwidth):
        target_ip = dst.IP()
        # Chỉnh lại tham số iperf một chút để đảm bảo chạy ổn định
        if traffic_type == 'video':
            bw_target = bandwidth * 1.2
            cmd = f'iperf -c {target_ip} -u -b {bw_target}M -t {POLLING_INTERVAL} -p 5001 &'
        elif traffic_type == 'voip':
            bw_target = 0.5 + (bandwidth * 0.05)
            cmd = f'iperf -c {target_ip} -u -b {bw_target}M -t {POLLING_INTERVAL} -p 5002 &'
        elif traffic_type == 'web':
            cmd = f'iperf -c {target_ip} -t {POLLING_INTERVAL} -p 80 &'
        else: 
            bw_target = bandwidth * 0.5
            cmd = f'iperf -c {target_ip} -u -b {bw_target}M -t {POLLING_INTERVAL} -p 5003 &'
        
        src.cmd(cmd)

def run():
    setLogLevel('info')
    topo = ExpandedTopo()
    c0 = RemoteController(name='c0', ip=CONTROLLER_IP, port=CONTROLLER_PORT)
    net = Mininet(topo=topo, controller=c0, link=TCLink, switch=OVSSwitch)

    try:
        net.start()
        
        # --- CẤU HÌNH QUAN TRỌNG: BẬT STP ---
        info("*** Enabling STP on all switches to prevent Loops...\n")
        for s in net.switches:
            # Bật OpenFlow 1.3
            s.cmd(f'ovs-vsctl set Bridge {s.name} protocols=OpenFlow13')
            # Bật STP
            s.cmd(f'ovs-vsctl set Bridge {s.name} stp_enable=true')
        
        info("*** Waiting 45s for STP convergence (DO NOT SKIP)...\n")
        # Phải chờ STP hội tụ, nếu không pingAll sẽ fail
        time.sleep(45)
        
        info("*** Pinging all to learn MAC addresses...\n")
        net.pingAll()
        
        # --- KHỞI ĐỘNG SERVER ---
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
