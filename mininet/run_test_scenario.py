import sys
import time
import re
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
    """Topology 5 đường dẫn (Giống hệt lúc Training)"""
    def build(self):
        s_src = self.addSwitch('s_src', dpid='1') 
        s_dst = self.addSwitch('s_dst', dpid='2') 
        link_opts = dict(bw=BW_LIMIT, delay='5ms', loss=0, max_queue_size=1000, use_htb=True)

        # Hosts: Port 1-4
        for i in range(1, 5):
            client = self.addHost(f'h_src_{i}')
            self.addLink(client, s_src, port2=i, **link_opts)
        for i in range(1, 5):
            server = self.addHost(f'h_dst_{i}')
            self.addLink(s_dst, server, **link_opts)

        # Uplinks: Port 5-9
        for i in range(1, 6):
            dpid_val = str(10 + i) 
            sw_path = self.addSwitch(f's_path_{i}', dpid=dpid_val)
            src_port = 4 + i 
            self.addLink(s_src, sw_path, port1=src_port, **link_opts)
            self.addLink(sw_path, s_dst, **link_opts)

def parse_iperf(output):
    """Hàm đọc kết quả iperf để lấy băng thông"""
    # Tìm chuỗi kiểu " 9.50 Mbits/sec"
    m = re.search(r'(\d+\.?\d*)\s+Mbits/sec', output)
    if m:
        return float(m.group(1))
    return 0.0

def run_scenarios():
    setLogLevel('info')
    topo = TestTopo()
    c0 = RemoteController(name='c0', ip=CONTROLLER_IP, port=CONTROLLER_PORT)
    net = Mininet(topo=topo, controller=c0, link=TCLink, switch=OVSSwitch)

    try:
        info("\n*** [INIT] Starting Network & Disabling STP...\n")
        net.start()
        for s in net.switches:
            s.cmd(f'ovs-vsctl set Bridge {s.name} protocols=OpenFlow13')
            s.cmd(f'ovs-vsctl set Bridge {s.name} stp_enable=false')
        
        info("*** Waiting 5s for Controller connection...\n")
        time.sleep(5)

        # Lấy đối tượng Host để điều khiển
        h_src_1, h_src_2 = net.get('h_src_1', 'h_src_2')
        h_src_4 = net.get('h_src_4') # Dùng cho Background
        h_dst_1, h_dst_2 = net.get('h_dst_1', 'h_dst_2')
        h_dst_4 = net.get('h_dst_4')

        # Khởi động Server lắng nghe
        info("*** Starting Iperf Servers...\n")
        h_dst_1.cmd('iperf -s -p 5001 &') # Video Port
        h_dst_1.cmd('iperf -s -p 5002 &') # VoIP Port
        h_dst_4.cmd('iperf -s -p 5003 &') # Background Port
        
        # ==========================================
        # KỊCH BẢN 1: PHÂN LOẠI & KẾT NỐI
        # ==========================================
        info("\n" + "="*40 + "\n")
        info(">>> KỊCH BẢN 1: KIỂM TRA PHÂN LOẠI TRAFFIC (AI CLASS)\n")
        info("1. Ping Test (Làm ấm mạng)...\n")
        net.ping([h_src_1, h_dst_1])
        
        info("2. Gửi luồng VIDEO (UDP 5001)...\n")
        # Video: 10M, packet size 1400
        output = h_src_1.cmd('iperf -c 10.0.0.5 -u -b 10M -l 1400 -p 5001 -t 5')
        bw = parse_iperf(output)
        info(f"-> Kết quả Video: {bw} Mbits/sec\n")
        if bw > 8: 
            info("   [PASS] Video băng thông tốt (Controller đã ưu tiên).\n")
        else:
            info("   [WARN] Video băng thông thấp.\n")

        info("3. Gửi luồng VOIP (UDP 5002)...\n")
        output = h_src_2.cmd('iperf -c 10.0.0.5 -u -b 0.5M -l 100 -p 5002 -t 5')
        info("-> Đã gửi VoIP (Quan sát log Controller để thấy nhãn 'VOIP').\n")

        # ==========================================
        # KỊCH BẢN 2: DỰ ĐOÁN & CÂN BẰNG TẢI (AI PREDICT)
        # ==========================================
        info("\n" + "="*40 + "\n")
        info(">>> KỊCH BẢN 2: KIỂM TRA CÂN BẰNG TẢI THÔNG MINH (SMART LB)\n")
        
        info("BƯỚC 1: Gây nghẽn đường truyền bằng Luồng 1 (h_src_1 -> Path X)...\n")
        # Chạy ngầm 20s
        h_src_1.cmd('iperf -c 10.0.0.5 -u -b 25M -l 1400 -p 5001 -t 20 &')
        
        info("... Đợi 5 giây cho LSTM học và dự đoán tải cao ...\n")
        time.sleep(5)
        
        info("BƯỚC 2: Bắn tiếp Luồng 2 (h_src_2) vào mạng đang nghẽn...\n")
        # Nếu LB tốt, luồng 2 sẽ được lái sang đường khác và băng thông vẫn cao
        start_time = time.time()
        output_2 = h_src_2.cmd('iperf -c 10.0.0.5 -u -b 25M -l 1400 -p 5001 -t 10')
        
        bw_2 = parse_iperf(output_2)
        info(f"-> Kết quả Luồng 2 (Người đến sau): {bw_2} Mbits/sec\n")
        
        info("\n--- ĐÁNH GIÁ KẾT QUẢ ---\n")
        if bw_2 > 15:
            info("[SUCCESS] TUYỆT VỜI! Smart Controller đã chuyển hướng Luồng 2 sang đường trống.\n")
            info("          (Nếu không có AI, Luồng 2 sẽ chen vào đường 1 và chỉ đạt < 10Mbps)\n")
        elif bw_2 > 10:
            info("[OK] Tạm ổn. Có chia sẻ băng thông nhưng chưa tối ưu hoàn toàn.\n")
        else:
            info("[FAIL] Thất bại. Luồng 2 bị nghẽn (Có thể AI chưa kịp dự đoán hoặc Routing lỗi).\n")

        # ==========================================
        # KỊCH BẢN 3: BACKGROUND NOISE
        # ==========================================
        info("\n" + "="*40 + "\n")
        info(">>> KỊCH BẢN 3: KIỂM TRA ỔN ĐỊNH VỚI BACKGROUND TRAFFIC\n")
        
        info("Chạy Background Noise (Port 5003)...\n")
        h_src_4.cmd('iperf -c 10.0.0.8 -u -b 10M -l 500 -p 5003 -t 10 &')
        
        info("Chạy Video chính (Port 5001)...\n")
        output_bg = h_src_1.cmd('iperf -c 10.0.0.5 -u -b 15M -l 1400 -p 5001 -t 10')
        bw_bg = parse_iperf(output_bg)
        
        info(f"-> Băng thông Video khi có Noise: {bw_bg} Mbits/sec\n")
        if bw_bg > 12:
             info("[PASS] Video không bị ảnh hưởng bởi Background (QoS tốt).\n")

    except Exception as e:
        info(f"\n*** Error: {e}\n")
    finally:
        info("\n*** Stopping network & Cleaning up\n")
        net.stop()
        # Xóa các process iperf còn sót lại
        import os
        os.system('killall iperf')

if __name__ == '__main__':
    run_scenarios()
