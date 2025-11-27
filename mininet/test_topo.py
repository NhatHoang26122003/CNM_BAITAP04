import sys
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from mininet.topo import Topo
from mininet.log import setLogLevel, info
from mininet.cli import CLI

# --- CẤU HÌNH ---
CONTROLLER_IP = '127.0.0.1'
CONTROLLER_PORT = 6633
BW_LIMIT = 20          # Giới hạn băng thông để dễ test nghẽn mạng

class TestTopo(Topo):
    """
    Topology 5 đường dẫn (Giống hệt lúc Training)
    """
    def build(self):
        # 1. Switch Gốc và Đích
        # Dpid giữ nguyên như lúc training để Controller nhận diện đúng
        s_src = self.addSwitch('s_src', dpid='1') 
        s_dst = self.addSwitch('s_dst', dpid='2') 
        
        link_opts = dict(bw=BW_LIMIT, delay='5ms', loss=0, max_queue_size=1000, use_htb=True)

        # 2. Hosts (4 Client - 4 Server)
        for i in range(1, 5):
            client = self.addHost(f'h_src_{i}')
            self.addLink(client, s_src, **link_opts)
            
        for i in range(1, 5):
            server = self.addHost(f'h_dst_{i}')
            self.addLink(s_dst, server, **link_opts)

        # 3. 5 Đường đi song song
        for i in range(1, 6):
            dpid_val = str(10 + i) 
            sw_path = self.addSwitch(f's_path_{i}', dpid=dpid_val)
            src_port = 4 + i 
            self.addLink(s_src, sw_path, port1=src_port, **link_opts)
            self.addLink(sw_path, s_dst, **link_opts)

def run():
    setLogLevel('info')
    topo = TestTopo()
    # Kết nối tới Smart Controller
    c0 = RemoteController(name='c0', ip=CONTROLLER_IP, port=CONTROLLER_PORT)
    net = Mininet(topo=topo, controller=c0, link=TCLink, switch=OVSSwitch)

    try:
        net.start()
        
        # --- CẤU HÌNH QUAN TRỌNG CHO TEST ---
        info("*** Setting OpenFlow 1.3 & DISABLING STP...\n")
        info("*** (Smart Controller will handle loops and routing)\n")
        
        for s in net.switches:
            s.cmd(f'ovs-vsctl set Bridge {s.name} protocols=OpenFlow13')
            # TẮT STP: Để Controller toàn quyền quyết định đường đi
            s.cmd(f'ovs-vsctl set Bridge {s.name} stp_enable=false')
        
        # Mở giao diện dòng lệnh để test thủ công
        info("*** Running CLI for Testing...\n")
        CLI(net)
        
    except Exception as e:
        info(f"\n*** Error: {e}\n")
    finally:
        info("*** Stopping network\n")
        net.stop()

if __name__ == '__main__':
    run()
