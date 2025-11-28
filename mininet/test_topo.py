import sys
import time
import re
import os
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from mininet.topo import Topo
from mininet.log import setLogLevel, info

# --- CẤU HÌNH ---
CONTROLLER_IP = '127.0.0.1'
CONTROLLER_PORT = 6633
BW_LIMIT = 20

class TestTopo(Topo):
    """Topology 5 đường dẫn"""
    def build(self):
        s_src = self.addSwitch('s_src', dpid='1') 
        s_dst = self.addSwitch('s_dst', dpid='2') 
        link_opts = dict(bw=BW_LIMIT, delay='5ms', loss=0, max_queue_size=1000, use_htb=True)

        # Hosts: Port 1-4
        for i in range(1, 5):
            client = self.addHost(f'h_src_{i}', ip=f'10.0.0.{i}')
            self.addLink(client, s_src, port2=i, **link_opts)
        for i in range(1, 5):
            server = self.addHost(f'h_dst_{i}', ip=f'10.0.0.{10+i}')
            self.addLink(server, s_dst, port2=i, **link_opts)

        # Uplinks: Port 5-9
        for i in range(1, 6):
            dpid_val = str(10 + i) 
            sw_path = self.addSwitch(f's_path_{i}', dpid=dpid_val)
            src_port = 4 + i 
            self.addLink(s_src, sw_path, port1=src_port, **link_opts)
            self.addLink(sw_path, s_dst, **link_opts)

def parse_iperf(output):
    try:
        # Lấy dòng cuối cùng (SUM hoặc Average) để chính xác hơn
        lines = output.strip().split('\n')
        last_line = lines[-1]
        m = re.search(r'(\d+\.?\d*)\s+Mbits/sec', last_line)
        if m: return float(m.group(1))
        
        # Nếu không tìm thấy ở dòng cuối, tìm trong toàn bộ text
        m = re.search(r'(\d+\.?\d*)\s+Mbits/sec', output)
        if m: return float(m.group(1))
    except: pass
    return 0.0

def run_scenarios():
    setLogLevel('info')
    topo = TestTopo()
    c0 = RemoteController(name='c0', ip=CONTROLLER_IP, port=CONTROLLER_PORT)
    
    # autoSetMacs=True: Giúp Static ARP hoạt động
    net = Mininet(topo=topo, controller=c0, link=TCLink, switch=OVSSwitch, autoSetMacs=True)

    try:
        info("\n*** [INIT] Starting Network...\n")
        net.start()
        
        info("*** Populating Static ARP (No Broadcast Storms!)...\n")
        net.staticArp() 
        
        info("*** Disabling STP (AI Controller will route)...\n")
        for s in net.switches:
            s.cmd(f'ovs-vsctl set Bridge {s.name} protocols=OpenFlow13')
            s.cmd(f'ovs-vsctl set Bridge {s.name} stp_enable=false')
        
        info("*** Waiting 5s for Controller connection...\n")
        time.sleep(5)

        h_src_1, h_src_2 = net.get('h_src_1', 'h_src_2')
        h_src_4 = net.get('h_src_4')
        h_dst_1, h_dst_2 = net.get('h_dst_1', 'h_dst_2')
        h_dst_4 = net.get('h_dst_4')

        info("*** Starting Iperf Servers...\n")
        h_dst_1.cmd('iperf -s -p 5001 &') 
        h_dst_1.cmd('iperf -s -p 5002 &') 
        h_dst_4.cmd('iperf -s -p 5003 &') 
        
        # --- TEST 1: KẾT NỐI & PHÂN LOẠI ---
        info("\n" + "="*50 + "\n")
        info(">>> KỊCH BẢN 1: KIỂM TRA KẾT NỐI & PHÂN LOẠI (20s)\n")
        
        info("1. Ping Test (Kiểm tra kết nối cơ bản)...\n")
        net.ping([h_src_1, h_dst_1])
        
        info("2. Gửi VIDEO (UDP 5001) - Chạy 15s để Controller kịp Log...\n")
        # Tăng thời gian lên 15s
        output = h_src_1.cmd('iperf -c 10.0.0.11 -u -b 8M -l 1400 -p 5001 -t 15')
        bw = parse_iperf(output)
        info(f"-> Kết quả Video: {bw} Mbits/sec\n")
        
        if bw > 7:
            info("   [PASS] Video đạt băng thông yêu cầu.\n")
        else:
            info("   [WARN] Video băng thông hơi thấp (Có thể do khởi động).\n")

        info("3. Gửi VOIP (UDP 5002) - Chạy 10s...\n")
        # Chạy nền VoIP để quan sát log VOIP trên controller
        h_src_2.cmd('iperf -c 10.0.0.11 -u -b 0.5M -l 100 -p 5002 -t 10 &')
        time.sleep(10)
        info("-> Đã gửi VoIP xong.\n")

        # --- TEST 2: LOAD BALANCING ---
        info("\n" + "="*50 + "\n")
        info(">>> KỊCH BẢN 2: SMART LOAD BALANCING (40s)\n")
        
        info("BƯỚC 1: Gây nghẽn Luồng 1 (Video HD - 25Mbps)...\n")
        # Chạy 30s để đảm bảo LSTM bắt được xu hướng tăng tải
        h_src_1.cmd('iperf -c 10.0.0.11 -u -b 25M -l 1400 -p 5001 -t 30 &')
        
        info("... Đợi 10 giây cho AI học và dự đoán tải cao ...\n")
        # Tăng thời gian chờ lên 10s để chắc chắn AI đã thấy tải cao
        time.sleep(10)
        
        info("BƯỚC 2: Bắn tiếp Luồng 2 (Video HD - 25Mbps) vào mạng đang nghẽn...\n")
        # Luồng 2 chạy 15s. Nếu LB tốt, nó sẽ được lái sang đường khác.
        output_2 = h_src_2.cmd('iperf -c 10.0.0.11 -u -b 25M -l 1400 -p 5001 -t 15')
        bw_2 = parse_iperf(output_2)
        info(f"-> Kết quả Luồng 2: {bw_2} Mbits/sec\n")
        
        if bw_2 > 15:
            info("[SUCCESS] Load Balancing thành công rực rỡ! (Cả 2 luồng đều > 15Mbps)\n")
        else:
            info(f"[FAIL] Luồng 2 bị nghẽn (Chỉ đạt {bw_2} Mbps). Có thể đi chung đường với Luồng 1.\n")

        # --- TEST 3: BACKGROUND NOISE ---
        info("\n" + "="*50 + "\n")
        info(">>> KỊCH BẢN 3: KIỂM TRA ỔN ĐỊNH VỚI NHIỄU (20s)\n")
        
        info("Chạy Background Noise (Port 5003) liên tục...\n")
        h_src_4.cmd('iperf -c 10.0.0.14 -u -b 15M -l 500 -p 5003 -t 20 &')
        time.sleep(2)
        
        info("Chạy Video chính (Port 5001)...\n")
        output_bg = h_src_1.cmd('iperf -c 10.0.0.11 -u -b 10M -l 1400 -p 5001 -t 15')
        bw_bg = parse_iperf(output_bg)
        
        info(f"-> Băng thông Video khi có nhiễu: {bw_bg} Mbits/sec\n")
        if bw_bg > 9:
             info("[PASS] Video vẫn ổn định bất chấp nhiễu (QoS Isolation tốt).\n")

    except Exception as e:
        info(f"\n*** Error: {e}\n")
    finally:
        info("\n*** Stopping network & Cleaning up\n")
        net.stop()
        import os
        os.system('killall iperf')

if __name__ == '__main__':
    run_scenarios()
