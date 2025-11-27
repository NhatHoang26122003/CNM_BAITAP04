import time
import random
import math
import os
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from my_topo import MyTopo

def generate_smart_traffic(net):
    h1 = net.get('h1')
    h2 = net.get('h2')
    h3 = net.get('h3')
    h4 = net.get('h4')

    print("*** Bắt đầu sinh traffic thông minh (Hybrid: Classify + Predict)...")
    
    # Tổng thời gian chạy mô phỏng (ví dụ: 200 chu kỳ ~ 1000s)
    total_duration = 5 
    
    for t in range(total_duration):
        
        # --- PHẦN 1: TẠO PATTERN CHO BÀI TOÁN PREDICT ---
        # Sử dụng hàm Sin để tạo chu kỳ tăng giảm băng thông (Mô phỏng giờ cao điểm/thấp điểm)
        # bw_base: băng thông nền thay đổi theo thời gian t
        # math.sin(t / 10): Tạo sóng sin chu kỳ rộng
        # abs(...) để đảm bảo băng thông luôn dương
        # + 2: Đảm bảo tối thiểu 2Mbps
        
        bw_pattern = abs(15 * math.sin(t / 10.0)) + 5 
        
        # Thêm nhiễu ngẫu nhiên (Noise) để dữ liệu thực tế hơn (không quá mượt)
        noise = random.uniform(-2, 2)
        current_bandwidth = max(1, bw_pattern + noise) # Mbps (không bao giờ < 1)
        
        # Thỉnh thoảng tạo đột biến (Burst) - giả lập Flash Crowd
        if random.random() < 0.05: # 5% cơ hội xảy ra đột biến
            print(f"[Time {t}] !!! BURST TRAFFIC DETECTED !!!")
            current_bandwidth += 20 # Tăng vọt băng thông
            
        # --- PHẦN 2: CHỌN LOẠI TRAFFIC CHO BÀI TOÁN CLASSIFY ---
        # Chúng ta vẫn chọn loại traffic, nhưng băng thông (-b) sẽ tuân theo pattern ở trên
        
        traffic_type = random.choice(['video', 'voip', 'web'])
        
        # Format lệnh iperf: -t 4 (chạy 4s), nghỉ 1s để lặp vòng lặp mới
        # Chú ý: Ta ép băng thông (-b) theo biến current_bandwidth đã tính toán
        
        if traffic_type == 'video':
            # Video thường chiếm băng thông lớn hơn mức nền một chút, dùng UDP
            bw_video = current_bandwidth * 1.5 
            print(f"[Time {t}] Gen VIDEO | BW Target: {bw_video:.2f}M")
            h1.cmd(f'iperf -c {h2.IP()} -u -b {bw_video}M -t 4 -p 5001 &')
            
        elif traffic_type == 'voip':
            # VoIP băng thông nhỏ nhưng ổn định, dùng UDP
            # VoIP ít bị ảnh hưởng bởi pattern tải chung, nhưng ta cứ biến thiên nhẹ
            bw_voip = 0.5 + (current_bandwidth * 0.1) 
            print(f"[Time {t}] Gen VOIP  | BW Target: {bw_voip:.2f}M")
            h1.cmd(f'iperf -c {h2.IP()} -u -b {bw_voip}M -t 4 -p 5002 &')
            
        elif traffic_type == 'web':
            # Web dùng TCP, không giới hạn băng thông (-b) trong iperf TCP thường tự max
            # Nhưng để kiểm soát data cho đẹp, ta có thể dùng UDP mô phỏng Web traffic tải nặng
            # Hoặc dùng TCP mặc định
            print(f"[Time {t}] Gen WEB   | TCP flow")
            h1.cmd(f'iperf -c {h2.IP()} -t 4 -p 80 &')
            # Tạo thêm lưu lượng nền từ h3 -> h4 để làm mạng thêm bận rộn theo pattern
            h3.cmd(f'iperf -c {h4.IP()} -u -b {current_bandwidth}M -t 4 &')

        time.sleep(5) # Chờ iperf chạy xong đợt này rồi mới qua t tiếp theo

def run():
    topo = MyTopo()
    # Link phải có giới hạn bw (ví dụ 50Mbps) thì mới thấy nghẽn khi traffic tăng
    net = Mininet(topo=topo, controller=None, link=TCLink, switch=OVSSwitch) 
    for s in net.switches:
        s.cmd('ovs-vsctl set Bridge %s protocols=OpenFlow13' % s.name)
    c0 = net.addController(name='c0', 
                           controller=RemoteController, 
                           ip='127.0.0.1', 
                           port=6633)
    net.start()
    
    # Cấu hình BW cho link để dễ demo nghẽn mạng (QUAN TRỌNG CHO RL SAU NÀY)
    # Ví dụ: Link s1-s2 chỉ chịu được 20M, nếu traffic > 20M -> Packet Loss -> RL phải routing qua s3
    
    print("*** Waiting for controller...")
    time.sleep(5)

    net.pingAll()

    generate_smart_traffic(net)
    
    print("*** Stopping network")
    net.stop()

if __name__ == '__main__':
    run()
