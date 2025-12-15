from mininet.topo import Topo
from mininet.net import Mininet
from mininet.node import RemoteController
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel
import os

class FivePathTopo(Topo):
    def build(self):
        print("Creating 5-Path Topology (Matches SmartController)...")
        
        # --- Tạo Switch Chính ---
        # s1: Source Switch (DPID=1 để khớp với Controller dpid==1)
        s1 = self.addSwitch('s1', dpid='0000000000000001')
        # s2: Destination Switch (Gom các đường lại)
        s_dst = self.addSwitch('s2', dpid='0000000000000002')

        # --- Tạo Hosts ---
        # H1: Client (Gửi traffic)
        h1 = self.addHost('h1', ip="10.0.0.1", mac="00:00:00:00:00:01")
        # H2: Server (Nhận traffic)
        h2 = self.addHost('h2', ip="10.0.0.2", mac="00:00:00:00:00:02")

        # --- Kết nối Hosts vào Switch biên ---
        # H1 nối vào S1 ở Port 1 (Khớp điều kiện in_port <= 4 của Controller)
        self.addLink(h1, s1, port2=1, bw=100)
        self.addLink(s_dst, h2, bw=100)

        # --- Tạo 5 Đường Uplink (Middle Switches) ---
        # Controller quy định uplink_ports = [5, 6, 7, 8, 9]
        # Ta sẽ tạo 5 switch trung gian tương ứng
        uplink_ports = [5, 6, 7, 8, 9]
        
        for i, port in enumerate(uplink_ports):
            # Tạo switch trung gian s11, s12, s13, s14, s15
            path_sw = self.addSwitch(f's{11+i}') 
            
            # KẾT NỐI QUAN TRỌNG:
            # S1 nối ra Path Switch tại đúng Port 5, 6, 7, 8, 9
            # Băng thông set thấp (10Mbps) để dễ test nghẽn mạch
            self.addLink(s1, path_sw, port1=port, bw=10)
            
            # Path Switch nối về S_DST
            self.addLink(path_sw, s_dst, bw=10)

def run():
    topo = FivePathTopo()
    net = Mininet(topo=topo, controller=None, link=TCLink)
    net.addController('c0', controller=RemoteController, ip='127.0.0.1', port=6633)
    
    net.start()

    print("\n--- [CAU HINH] Kich hoat Static ARP (Fix loi Ping) ---")
    net.staticArp() 

    print("--- [CAU HINH] Thiet lap QoS Queues cho 5 cong Uplink ---")
    # Cấu hình hàng đợi QoS cho các cổng s1-eth5 đến s1-eth9
    for port_num in [5, 6, 7, 8, 9]:
        interface = f's1-eth{port_num}'
        # Tạo 2 hàng đợi: q0 (Default - TCP), q1 (Priority - Video/UDP)
        # Max rate 100M, q0 min 1M, q1 min 5M (ưu tiên)
        cmd = f'ovs-vsctl -- set Port {interface} qos=@newqos -- \
                --id=@newqos create QoS type=linux-htb other-config:max-rate=100000000 queues=0=@q0,1=@q1 -- \
                --id=@q0 create Queue other-config:min-rate=1000000 other-config:max-rate=5000000 -- \
                --id=@q1 create Queue other-config:min-rate=5000000 other-config:max-rate=100000000'
        os.system(cmd)

    print(f"\n*** Topology san sang! ***")
    print(f"    - S1 Uplink Ports: 5, 6, 7, 8, 9 (Tuong ung Path 0-4 trong AI)")
    print(f"    - H1 -> H2: Ping se hoat dong ngay lap tuc.")
    
    CLI(net)
    net.stop()

if __name__ == '__main__':
    setLogLevel('info')
    run()
