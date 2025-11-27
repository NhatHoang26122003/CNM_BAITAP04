from mininet.topo import Topo

class MyTopo(Topo):
    "Simple topology example with Traffic Engineering constraints."

    def build(self):
        # Add hosts and switches
        h1 = self.addHost('h1')
        h2 = self.addHost('h2')
        h3 = self.addHost('h3')
        h4 = self.addHost('h4')

        s1 = self.addSwitch('s1')
        s2 = self.addSwitch('s2')
        s3 = self.addSwitch('s3')
        s4 = self.addSwitch('s4')

        # Cấu hình Link: 
        # bw=20: Băng thông 20Mbps (Đây là "ngưỡng" để AI dự đoán nghẽn)
        # delay='5ms': Độ trễ giả lập
        # max_queue_size=1000: Kích thước hàng đợi (quan trọng để đo loss)
        
        link_opts = dict(bw=20, delay='5ms', loss=0, max_queue_size=1000, use_htb=True)
        
        # Đường trục chính (Backbone) dễ bị nghẽn -> Cần cấu hình kỹ
        # h1 -> s1 -> [s2, s3] -> s4 -> h2
        
        # Host links (Thường băng thông cao hơn hoặc bằng backbone)
        self.addLink(h1, s1, **link_opts)
        self.addLink(h1, s2, **link_opts) 
        
        # Switch-to-Switch links (Nơi xảy ra nghẽn và Routing)
        self.addLink(s1, s2, **link_opts)
        self.addLink(s1, s3, **link_opts)
        self.addLink(s2, s4, **link_opts)
        self.addLink(s3, s4, **link_opts)
        
        self.addLink(s4, h2, **link_opts)
        self.addLink(s3, h3, **link_opts)
        self.addLink(s2, h4, **link_opts)

topos = { 'mytopo': ( lambda: MyTopo() ) }
