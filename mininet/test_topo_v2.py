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
    """Hàm parse thông minh, xử lý đơn vị Gbits/Mbits/Kbits"""
    try:
        matches = re.findall(r'(\d+\.?\d*)\s+([KMG]bits/sec)', output)
        if not matches:
            return 0.0
        val_str, unit = matches[-1]
        val = float(val_str)
        if unit.startswith('G'): return val * 1000
        elif unit.startswith('K'): return val / 1000
        else: return val
    except Exception as e:
        return 0.0

def run_scenarios():
    setLogLevel('info')
    topo = TestTopo()
    c0 = RemoteController(name='c0', ip=CONTROLLER_IP, port=CONTROLLER_PORT)
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

        # Lấy các host nguồn
        h_src_1, h_src_2, h_src_3 = net.get('h_src_1', 'h_src_2', 'h_src_3')
        h_src_4 = net.get('h_src_4')
        
        # [UPDATE] Lấy danh sách tất cả host đích để bật Server
        dst_hosts = [net.get(f'h_dst_{i}') for i in range(1, 5)] 

        info("*** Starting Iperf Servers on ALL destination hosts...\n")
        for h in dst_hosts:
            # [FIX] Thêm cờ -u cho các cổng UDP để Server lắng nghe đúng giao thức
            h.cmd('iperf -s -u -p 5001 &') # Video (UDP)
            h.cmd('iperf -s -u -p 5002 &') # VoIP (UDP)
            h.cmd('iperf -s -u -p 5003 &') # Background (UDP)
            
            # Cổng Web dùng TCP thì không cần -u
            h.cmd('iperf -s -p 80 &')      # Web (TCP)
        
        # Gán biến để dùng bên dưới cho tiện
        h_dst_1 = dst_hosts[0] 
        h_dst_4 = dst_hosts[3]

        # --- TEST 1 ---
        info("\n" + "="*50 + "\n")
        info(">>> KỊCH BẢN 1: PHÂN LOẠI VIDEO & VOIP\n")
        net.ping([h_src_1, h_dst_1])
        
        info("2. Gửi VIDEO (UDP 5001)...\n")
        output = h_src_1.cmd('iperf -c 10.0.0.11 -u -b 8M -l 1400 -p 5001 -t 10')
        bw = parse_iperf(output)
        info(f"-> Video BW: {bw} Mbps\n")

        info("3. Gửi VOIP (UDP 5002)...\n")
        h_src_2.cmd('iperf -c 10.0.0.11 -u -b 0.5M -l 100 -p 5002 -t 10')
        info("-> Đã gửi VoIP (Check log Controller).\n")

        # --- TEST WEB (MỚI) ---
        info("\n" + "="*50 + "\n")
        info(">>> KỊCH BẢN MỚI: KIỂM TRA WEB TRAFFIC\n")
        info("Gửi traffic WEB (TCP Port 80) từ h_src_2...\n")
        output_web = h_src_2.cmd('iperf -c 10.0.0.11 -p 80 -t 10')
        bw_web = parse_iperf(output_web)
        info(f"-> Web Traffic BW: {bw_web} Mbps\n")
        info("   (Quan sát Controller: Bạn sẽ thấy [AI CLASSIFIER] Flow WEB)\n")

        # --- TEST 2 ---
        info("\n" + "="*50 + "\n")
        info(">>> KỊCH BẢN 2: SMART LOAD BALANCING (LSTM)\n")
        
        info("BƯỚC 1: Gây nghẽn Luồng 1 (25Mbps)...\n")
        h_src_1.cmd('iperf -c 10.0.0.11 -u -b 25M -l 1400 -p 5001 -t 30 &')
        
        info("... Đợi 5s cho LSTM ổn định xu hướng ...\n")
        time.sleep(5)
        
        info("BƯỚC 2: Bắn Luồng 2 (25Mbps) vào lúc nghẽn...\n")
        start = time.time()
        output_2 = h_src_2.cmd('iperf -c 10.0.0.11 -u -b 25M -l 1400 -p 5001 -t 15')
        bw_2 = parse_iperf(output_2)
        info(f"-> Luồng 2 BW: {bw_2} Mbps\n")
        
        if bw_2 > 15:
            info("[SUCCESS] Load Balancing tốt (>15Mbps).\n")
        else:
            info("[FAIL] Bị nghẽn.\n")

        # --- TEST 3: MIXED TRAFFIC (Kịch bản mới) ---
        info("\n" + "="*50 + "\n")
        info(">>> KỊCH BẢN 3: HỖN HỢP (VIDEO + VOIP + WEB)\n")
        info("Chạy đồng thời 3 luồng traffic...\n")
        
        # Chạy nền 2 luồng (VoIP và Web)
        h_src_2.cmd('iperf -c 10.0.0.11 -u -b 0.5M -l 100 -p 5002 -t 15 &') # VoIP
        h_src_3.cmd('iperf -c 10.0.0.11 -p 80 -t 15 &')                    # Web
        
        # Chạy chính Video để xem kết quả
        output_mix = h_src_1.cmd('iperf -c 10.0.0.11 -u -b 15M -l 1400 -p 5001 -t 15')
        bw_mix = parse_iperf(output_mix)
        
        info(f"-> Video trong môi trường hỗn hợp: {bw_mix} Mbps\n")
        info("   (Quan sát Controller: Bạn sẽ thấy AI phân loại và xử lý cả 3 luồng cùng lúc)\n")

    except Exception as e:
        info(f"\n*** Error: {e}\n")
    finally:
        info("\n*** Stopping network\n")
        net.stop()
        os.system('killall iperf')

if __name__ == '__main__':
    run_scenarios()
