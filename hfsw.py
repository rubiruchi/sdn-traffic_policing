from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.topology import event
from ryu.topology.api import get_switch, get_link, get_host
from ryu.lib.packet import packet, ethernet, arp, ipv4
import networkx as nx
import policy_manager
from policy_inputs import generate_policies
from ryu.lib import hub
from operator import attrgetter
from collections import defaultdict

links = []
switch_list = []
links_list = []
sleeptime = 5
bandwidth_limit = 10
port_errors = defaultdict(lambda:defaultdict(lambda:None))
port_status = defaultdict(lambda:defaultdict(lambda:None))
old_src_tx_bytes = defaultdict(lambda:defaultdict(lambda:None))
old_src_rx_bytes = defaultdict(lambda:defaultdict(lambda:None))
old_dst_tx_bytes = defaultdict(lambda:defaultdict(lambda:None))
old_dst_rx_bytes = defaultdict(lambda:defaultdict(lambda:None))

class HFsw(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(HFsw, self).__init__(*args, **kwargs)
        self.mac_to_port = {}
        self.topology_api_app = self
        self.net=nx.DiGraph()
        self.monitor_thread = hub.spawn(self._network_monitor)
        self.new_flow_stats = 0

        #Executes the policies at initiation
        #generate_policies()
        global policy_list
        policy_list=generate_policies()



    #Listens for incoming packets to the controller
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        pkt = packet.Packet(msg.data)
        dp = msg.datapath
        eth = pkt.get_protocols(ethernet.ethernet)[0]
        dpid = dp.id
        dst = eth.dst
        src = eth.src
        in_port = msg.match['in_port']
        ofp_parser = dp.ofproto_parser
        arp_pkt = pkt.get_protocol(arp.arp)

        if arp_pkt:
            #print "ARP request received at sw:",dpid
            if src not in self.net: #Learn it
                self.net.add_node(src) # Add a node to the graph
                self.net.add_edge(src,dpid) # Add a link from the node to it's edge switch
                self.net.add_edge(dpid,src,{'port':in_port})  # Add link from switch to node and make sure you are identifying the output port.
                print "Node added to grap, Src:", src, " connected to Sw:", dpid, " on port: ",in_port

            if dst in self.net:
                if nx.has_path(self.net, src, dst):
                    try:
                        #Find policies
                        policy_manager.policy_finder(pkt, policy_list)
                        self.network_checker(src, dst, "lol")
                        #Gets the shortest path
                        path=nx.shortest_path(self.net,src,dst)
                        self.install_flows(path)

                    except nx.NetworkXNoPath:
                        print "Could not create flow path"
                else:
                    print "No path found between ", src, " and ", dst

            else:
                #Iterates over the switches and sends ARP requests on all ports, except links connecting other switches.
                #(In order to avoid arp broadcast loops).
                print "Flooding ARP"

                for node in switch_list:
                            for n in node.ports:
                                host_port = True
                                for l in links:
                                    #If it is a link connecting two switches
                                    if l[0] == node.dp.id and l[2]['port'] == n.port_no:
                                        host_port = False
                                        break
                                    #If it is the port where the request is sent from
                                    elif node.dp.id == dpid and n.port_no == in_port:
                                        host_port = False
                                        break

                                if host_port:
                                    actions = [ofp_parser.OFPActionOutput(port=n.port_no)]
                                    out = ofp_parser.OFPPacketOut(datapath=node.dp, buffer_id=0xffffffff, in_port=in_port, actions=actions, data=msg.data)
                                    node.dp.send_msg(out)
                                    print "ARP forwarded on sw:", node.dp.id, " out port: ", n.port_no





    #Listens for connecting switches (ConnectionUp)
    @set_ev_cls(event.EventSwitchEnter)
    def get_topology_data(self, ev):
        global links, switch_list, links_list
        switch_list = get_switch(self.topology_api_app, None)
        switches=[switch.dp.id for switch in switch_list]
        links_list = get_link(self.topology_api_app, None)
        #links=[(link.src.dpid,link.dst.dpid,{'port':link.src.port_no}) for link in links_list]

        #Updates graph every time we get topology data
        self.net.add_nodes_from(switches)
        #self.net.add_edges_from(links)
        print "Switch oppdaget: ", switches
        print "Link oppdaget", links



    #Add hosts to hosts_list, but only works at initiation
    @set_ev_cls(event.EventHostAdd)
    def get_host_data(self, ev):
        hosts_list = get_host(self.topology_api_app, None)
        print ev


    #Detects new links
    @set_ev_cls(event.EventLinkAdd)
    def _event_link_add_handler(self, ev):
        global links
        link = ev.link
        links.append((link.src.dpid,link.dst.dpid,{'port':link.src.port_no}))
        print "Link discovered between sw:", link.src.dpid, " and sw:", link.dst.dpid, ". Total number of active links: ",len(links_list)/2
        self.net.add_edges_from(links)


    @set_ev_cls(event.EventLinkDelete)
    def _event_link_delete_handler(self, ev):
        link = ev.link
        for l in links:
            if l[0] == link.src.dpid and l[1] == link.dst.dpid:
                links.pop(links.index(l))
            elif l[0] == link.dst.dpid and l[1] == link.src.dpid:
                links.pop(links.index(l))

        print "Link disconnected between sw:", link.src.dpid, " and sw:", link.dst.dpid, ". Total number of active links: ",len(links_list)/2
        #TODO: Rerouting prosedure




    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def _flow_stats_reply_handler(self, ev):
        body = ev.msg.body

        for stat in sorted([flow for flow in body if flow.priority == 1],key=lambda flow: (flow.match['in_port'],flow.match['eth_dst'])):
            self.logger.info('%016x %8x %17s %8x %8d %8d',
                             ev.msg.datapath.id,
                             stat.match['in_port'], stat.match['eth_dst'],
                             stat.instructions[0].actions[0].port,
                             stat.packet_count, stat.byte_count)



    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply_handler(self, ev):

        #Adding the port information to a global list
        global port_status
        port_status[ev.msg.datapath.id] = ev.msg.body




    @set_ev_cls(ofp_event.EventOFPPortDescStatsReply, MAIN_DISPATCHER)
    def port_desc_stats_reply_handler(self, ev):
        #self.logger.info('OFPPortDescStatsReply received: \n')
        """
        Port description reply message
        The switch responds with this message to a port description request.
        Attribute   |    Description
        ------------|---------------
        body        |    List of OFPPortDescStats instance
        """
        for p in ev.msg.body:
            print("\t port_no=%d hw_addr=%s name=%s config=0x%08x "
                             "\n \t state=0x%08x curr=0x%08x advertised=0x%08x "
                             "\n \t supported=0x%08x peer=0x%08x curr_speed=%d "
                             "max_speed=%d" %
                             (p.port_no, p.hw_addr,
                              p.name, p.config,
                              p.state, p.curr, p.advertised,
                              p.supported, p.peer, p.curr_speed,
                              p.max_speed))




################ CUSTOM FUNCTIONS ######################################################################################

    def install_flows(self, path):
        print "Installing flow rules in one path direction"

        #Sorts path to install flow rules in oposite direction
        path = path[::-1]
        mac_src=path[-1]
        mac_dst=path[0]

            #Install final destination (host to switch)
        try:
            out_port= self.net[path[1]][mac_dst]['port']
            self.send_flow_rule(mac_src, mac_dst, out_port, path[1])

        except KeyError:
            print "Error creating source flow rules"

            #Install intermediate path
        for node in range(len(path)):
            for l in links:
                try:
                    if node+2 < (len(path)-1) and l[0] == path[node+2] and l[1] == path[node+1]:
                        out_port = l[2]['port']
                        self.send_flow_rule(mac_src, mac_dst, out_port, path[node+2])
                except IndexError:
                    print "Iterating function out of range"



    #Iterates through network data and checks if network parameters is accepted by the policy.
    def network_checker(self, src, dst, policies):
        possible_paths=nx.all_shortest_paths(self.net,src,dst)

        for p in possible_paths:
            out_port= self.net[p[1]][p[0]]['port']
            print "Network Checker: Port: ", out_port, " Node", p[1]
            for node in range(len(p)):
                for l in links:
                    try:
                        if node+2 < (len(p)-1) and l[0] == p[node+2] and l[1] == p[node+1]:
                            out_port = l[2]['port']
                            print "Network Checker: Port: ", out_port, " Node ", p[node+2]
                    except IndexError:
                        print "Iterating function out of range"


    def _network_monitor(self):
        while True:
            for node in switch_list:
                self.send_stats_request(node.dp)

            for link in links_list:
                self.check_link(link, True, True)

            hub.sleep(sleeptime)


    #Iterating function to inspect a given link for packet loss and transmitting traffic
    def check_link(self, link, measure_loss, measure_bandwidth):
        global old_dst_tx_bytes, old_src_tx_bytes, old_src_tx_bytes, old_src_tx_bytes
        src_tx_bytes = 0
        dst_tx_bytes = 0
        src_rx_bytes = 0
        dst_rx_bytes = 0

        for stat in port_status[link.src.dpid]:
            if link.src.port_no == stat.port_no:
                src_rx_bytes = stat.rx_bytes
                src_tx_bytes = stat.tx_bytes

        for stat in port_status[link.dst.dpid]:
            if link.dst.port_no == stat.port_no:
                dst_rx_bytes = stat.rx_bytes
                dst_tx_bytes = stat.tx_bytes

        if old_src_tx_bytes[link.src.dpid][link.src.port_no] is None and old_dst_tx_bytes[link.dst.dpid][link.dst.port_no] \
                is None and old_src_rx_bytes[link.src.dpid][link.src.port_no] is None and old_dst_rx_bytes[link.dst.dpid][link.dst.port_no] is None:
            old_src_rx_bytes[link.src.dpid][link.src.port_no] = src_rx_bytes
            old_src_tx_bytes[link.src.dpid][link.src.port_no] = src_tx_bytes
            old_dst_rx_bytes[link.dst.dpid][link.dst.port_no] = dst_rx_bytes
            old_dst_tx_bytes[link.dst.dpid][link.dst.port_no] = dst_tx_bytes

        pathloss = abs(((old_src_tx_bytes[link.src.dpid][link.src.port_no]+old_src_rx_bytes[link.src.dpid][link.src.port_no]) - \
                   (old_dst_tx_bytes[link.dst.dpid][link.dst.port_no]+old_dst_rx_bytes[link.dst.dpid][link.dst.port_no])) - \
                   ((src_rx_bytes+src_tx_bytes)-(dst_rx_bytes+dst_tx_bytes)))

        old_traffic = (old_src_tx_bytes[link.src.dpid][link.src.port_no]+old_src_rx_bytes[link.src.dpid][link.src.port_no]+ \
                       old_dst_tx_bytes[link.dst.dpid][link.dst.port_no] + old_dst_rx_bytes[link.dst.dpid][link.dst.port_no])
        traffic = abs((8*float(old_traffic - (src_tx_bytes+src_rx_bytes+dst_tx_bytes+dst_rx_bytes))/4/1000000)/sleeptime)

        old_src_tx_bytes[link.src.dpid][link.src.port_no] = src_tx_bytes
        old_src_rx_bytes[link.src.dpid][link.src.port_no] = src_rx_bytes
        old_dst_tx_bytes[link.dst.dpid][link.dst.port_no] = dst_tx_bytes
        old_dst_rx_bytes[link.dst.dpid][link.dst.port_no] = dst_rx_bytes


        if measure_bandwidth and measure_loss:
            print traffic, " mbit/s and with packet loss of ", pathloss, "the last ", sleeptime, "seconds at link:", link.src.dpid, " - ", link.dst.dpid
            return pathloss, traffic

        elif measure_loss:
            print pathloss, "bytes lost the last ", sleeptime, "  seconds at link ", link.src.dpid, " - ", link.dst.dpid
            return pathloss

        elif measure_bandwidth:
            print traffic, " mbit/s at link:", link.src.dpid, " - ", link.dst.dpid
            return traffic


################ OPENFLOW MESSAGE FUNCTIONS ############################################################################


    #Function to send a flow rule to a switch
    def send_flow_rule(self,src, dst, out_port, sw):
        print "Installing rule on :", sw, "Match conditions: eth_src =  ", src, " and eth_dst = ", dst, ". Action: out_port =  ", out_port
        self.src = src
        self.dst = dst
        self.out_port = out_port
        self.sw = sw

        for node in switch_list:
            if node.dp.id == sw:
                ofp_parser = node.dp.ofproto_parser
                actions = [ofp_parser.OFPActionOutput(port=out_port)]
                match = node.dp.ofproto_parser.OFPMatch(eth_src=src, eth_dst=dst)
                inst = [ofp_parser.OFPInstructionActions(ofproto_v1_3.OFPIT_APPLY_ACTIONS, actions)]
                mod = node.dp.ofproto_parser.OFPFlowMod(datapath=node.dp, match=match, cookie=0,command=ofproto_v1_3.OFPFC_ADD, idle_timeout=0, hard_timeout=0,priority=ofproto_v1_3.OFP_DEFAULT_PRIORITY, instructions=inst)
                node.dp.send_msg(mod)


    #Function to send a group rule to a switch
    def send_group_rule(self, src, dst, sw):
        self.src = src
        self.dst = dst
        self.sw = sw

        for node in switch_list:
            if node.dp.id == sw:
                ofp_parser = node.dp.ofproto_parser
                port = 1
                max_len = 2000
                actions = [ofp_parser.OFPActionOutput(port, max_len)]

                weight = 100
                watch_port = 0
                watch_group = 0
                buckets = [ofp_parser.OFPBucket(weight, watch_port, watch_group,actions)]

                group_id = 1
                req = ofp_parser.OFPGroupMod(datapath=node.dp, command=ofproto_v1_3.OFPGC_ADD, type=ofproto_v1_3.OFPGT_SELECT, group_id=group_id, buckets=buckets)
                node.dp.send_msg(req)


    #Function to send a stats request to a switch
    def send_stats_request(self,datapath):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        #Sends a Flow Stats Request
        #req = parser.OFPFlowStatsRequest(datapath)
        #datapath.send_msg(req)

        #Sends a Port_Stats_Request
        req = parser.OFPPortStatsRequest(datapath, 0, ofproto.OFPP_ANY)
        datapath.send_msg(req)

        #req = parser.OFPGroupStatsRequest(datapath,0, ofproto.OFPG_ALL,None)
        #datapath.send_msg(req)

       # req = parser.OFPPortDescStatsRequest(datapath, 0)
        #datapath.send_msg(req)
        #print "sent to, ", datapath




#TODO: Check why it is so slow. Due to loss at ARP reply?
#TODO: Add group_mod function and test it
#TODO: Is it possible to add more actions to the flow rules. Look at how we can send a flow through multiple group tables
# http://csie.nqu.edu.tw/smallko/sdn/ryu_multipath_13.htm
#http://ryu-zhdoc.readthedocs.org/en/latest/ofproto_v1_3_ref.html
