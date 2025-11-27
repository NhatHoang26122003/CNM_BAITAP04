import time
import random
import math
import sys
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from mininet.topo import Topo
from mininet.log import setLogLevel, info

# --- CẤU HÌNH ---
CONTROLLER_IP = '127.0.0.1'
CONTROLLER_PORT = 6633
TOTAL_DURATION = 10  # Số chu kỳ chạy mô phỏng
POLLING_INTERVAL = 5  # Thời gian nghỉ giữa các lần sinh traffic (giây)

class MyTopo(Topo):
    """
    Topology mạng tùy chỉnh cho bài tập Traffic Engineering.
    Cấu trúc:
             h1 (Dual-homed to s1, s2)
            /  \
           s1--s2
           |    |
           s3--s4
          /      \
        h3        h2
                   \
                   h4 (Connected to s2 based on original code logic)
    """
    def build(self):
        # Tạo Hosts
        h1 = self.addHost('h1')
        h2 = self.addHost('h2')
        h3 = self.addHost('h3')
        h4 = self.addHost('h4')

        # Tạo Switches
        s1 = self.addSwitch('s1')
        s2 = self.addSwitch('s2')
        s3 = self.addSwitch('s3')
        s4 = self.addSwitch('s4')

        # Cấu hình Link: 
        # bw=20Mbps là ngưỡng quan trọng để train AI phát hiện nghẽn
        link_opts = dict(bw=20, delay='5ms', loss=0, max_queue_size=1000, use_htb=True)
        
        # Link Host -> Switch
        self.addLink(h1, s1, **link_opts)
        self.addLink(h1, s2, **link_opts) # Dual-homed h1
        self.addLink(s4, h2, **link_opts)
        self.addLink(s3, h3, **link_opts)
        self.addLink(s2, h4, **link_opts) # h4 nối vào s2

        # Link Inter-Switch (Backbone)
        self.addLink(s1, s2, **link_opts)
        self.addLink(s1, s3, **link_opts)
        self.addLink(s2, s4, **link_opts)
        self.addLink(s3, s4, **link_opts)

class TrafficGenerator:
    def __init__(self, net):
        self.net = net
        self.h1 = net.get('h1')
        self.h2 = net.get('h2')
        self.h3 = net.get('h3')
        self.h4 = net.get('h4')

    def generate(self, duration=TOTAL_DURATION):
        info(f"*** Bắt đầu sinh traffic thông minh trong {duration} chu kỳ...\n")
        
        for t in range(duration):
            # --- TẠO PATTERN (DỰ ĐOÁN) ---
            # Sóng Sin tạo chu kỳ cao điểm/thấp điểm
            # BW nền dao động từ 5Mbps đến 20Mbps
            bw_pattern = abs(15 * math.sin(t / 10.0)) + 5 
            
            # Thêm nhiễu (Noise) để dữ liệu tự nhiên hơn
            noise = random.uniform(-2, 2)
            current_bandwidth = max(1, bw_pattern + noise) # Đảm bảo > 1Mbps
            
            # Giả lập Flash Crowd (Đột biến) - 5% cơ hội
            if random.random() < 0.05:
                info(f"[Time {t:03d}] !!! BURST TRAFFIC DETECTED !!!\n")
                current_bandwidth += 20 
            
            # --- CHỌN LOẠI TRAFFIC (PHÂN LOẠI) ---
            traffic_type = random.choice(['video', 'voip', 'web'])
            
            # Gửi lệnh iperf
            self._send_traffic(traffic_type, current_bandwidth, t)
            
            # Tạo lưu lượng nền (Background traffic) để gây nhiễu mạng h3 -> h4
            # Giúp bài toán dự đoán nghẽn trở nên khó hơn và thực tế hơn
            self.h3.cmd(f'iperf -c {self.h4.IP()} -u -b {current_bandwidth}M -t 4 &')

            time.sleep(POLLING_INTERVAL)

    def _send_traffic(self, traffic_type, bandwidth, t):
        """Helper function để gửi lệnh iperf dựa trên loại traffic"""
        
        # Mapping cấu hình cho từng loại traffic
        # Port phải khớp với logic trong Controller (5001, 5002, 80)
        target_ip = self.h2.IP()
        
        if traffic_type == 'video':
            # Video: UDP, Bandwidth cao hơn mức nền (x1.5), Port 5001
            bw_target = bandwidth * 1.5
            info(f"[Time {t:03d}] Gen VIDEO | BW: {bw_target:.2f}M | Port: 5001\n")
            self.h1.cmd(f'iperf -c {target_ip} -u -b {bw_target}M -t 4 -p 5001 &')
            
        elif traffic_type == 'voip':
            # VoIP: UDP, Bandwidth thấp & ổn định, Port 5002
            bw_target = 0.5 + (bandwidth * 0.1)
            info(f"[Time {t:03d}] Gen VOIP  | BW: {bw_target:.2f}M | Port: 5002\n")
            self.h1.cmd(f'iperf -c {target_ip} -u -b {bw_target}M -t 4 -p 5002 &')
            
        elif traffic_type == 'web':
            # Web: TCP, Port 80
            info(f"[Time {t:03d}] Gen WEB   | TCP Flow      | Port: 80\n")
            self.h1.cmd(f'iperf -c {target_ip} -t 4 -p 80 &')

def run():
    # Set log level để thấy output in ra màn hình
    setLogLevel('info')
    
    topo = MyTopo()
    # Khởi tạo Controller từ xa (Ryu)
    c0 = RemoteController(name='c0', ip=CONTROLLER_IP, port=CONTROLLER_PORT)
    
    net = Mininet(topo=topo, controller=c0, link=TCLink, switch=OVSSwitch)

    try:
        net.start()
        
        # Cấu hình switch sử dụng OpenFlow 1.3
        for s in net.switches:
            s.cmd(f'ovs-vsctl set Bridge {s.name} protocols=OpenFlow13')

        info("*** Waiting for controller to connect (5s)...\n")
        time.sleep(5)
        
        # --- QUAN TRỌNG: Ping All để Controller học MAC Address ---
        # Nếu không có bước này, switch sẽ flood gói tin và flow stats sẽ không được ghi nhận
        info("*** Pinging all hosts to learn MAC addresses...\n")
        #net.pingAll()
        
        # Bắt đầu sinh traffic
        generator = TrafficGenerator(net)
        generator.generate()
        
    except KeyboardInterrupt:
        info("\n*** Interrupted by user\n")
    except Exception as e:
        info(f"\n*** Error: {e}\n")
    finally:
        info("*** Stopping network\n")
        net.stop()

if __name__ == '__main__':
    run()
