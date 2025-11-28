import sys
import time
import os
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from mininet.topo import Topo
from mininet.log import setLogLevel, info
from mininet.cli import CLI

# --- CẤU HÌNH ---
CONTROLLER_IP = '127.0.0.1'
CONTROLLER_PORT = 6633
BW_LIMIT = 20

class ManualTopo(Topo):
    """Topology 5 đường dẫn cho Test Thủ Công"""
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

def run():
    setLogLevel('info')
    topo = ManualTopo()
    c0 = RemoteController(name='c0', ip=CONTROLLER_IP, port=CONTROLLER_PORT)
    
    # autoSetMacs=True để Static ARP hoạt động chuẩn
    net = Mininet(topo=topo, controller=c0, link=TCLink, switch=OVSSwitch, autoSetMacs=True)

    try:
        info("\n*** [INIT] Starting Network...\n")
        net.start()
        
        info("*** Populating Static ARP (Ready for AI Routing)...\n")
        net.staticArp() 
        
        info("*** Disabling STP (Let Smart Controller handle loops)...\n")
        for s in net.switches:
            s.cmd(f'ovs-vsctl set Bridge {s.name} protocols=OpenFlow13')
            s.cmd(f'ovs-vsctl set Bridge {s.name} stp_enable=false')
        
        info("*** Waiting 5s for Controller connection...\n")
        time.sleep(5)

        # Khởi động sẵn Server để bạn tiện test
        info("*** Starting Background Iperf Servers (UDP & TCP)...\n")
        dst_hosts = [net.get(f'h_dst_{i}') for i in range(1, 5)]
        
        for h in dst_hosts:
            h.cmd('iperf -s -u -p 5001 &') # Video Port
            h.cmd('iperf -s -u -p 5002 &') # VoIP Port
            h.cmd('iperf -s -u -p 5003 &') # Background Port
            h.cmd('iperf -s -p 80 &')      # Web Port
            
        info("*** Servers are listening on ports: 5001, 5002, 5003 (UDP) and 80 (TCP)\n")
        info("*** Ready! Running CLI. Type 'exit' to quit.\n")
        
        # Mở giao diện dòng lệnh Mininet
        CLI(net)

    except Exception as e:
        info(f"\n*** Error: {e}\n")
    finally:
        info("\n*** Stopping network\n")
        net.stop()
        os.system('killall iperf')

if __name__ == '__main__':
    run()
