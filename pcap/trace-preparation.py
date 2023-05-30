import argparse
import re
import socket
import ipaddress
import struct
import os
import threading
import multiprocessing
import mmap
from atpbar import atpbar, register_reporter, find_reporter, flush
from progressbar import ProgressBar, Percentage, Bar, ETA, AdaptiveETA
import concurrent.futures 
import numpy as np

from randmac import RandMac
from scapy.layers.inet import IP, TCP, UDP, ICMP
from scapy.layers.l2 import Ether
from scapy.packet import Raw
from scapy.utils import wrpcap
from scapy.volatile import RandIP, RandString
from scapy.all import *

widgets = [Percentage(),
           ' ', Bar(),
           ' ', ETA(),
           ' ', AdaptiveETA()]

MAX_FILE_SIZE=1000000

pbar_update_value = 0
total_tasks = 0
class RawPcapReaderFD(RawPcapReader):
    """A stateful pcap reader. Each packet is returned as a string"""

    def __init__(self, fd):
        self.filename = "dummy"
        try:
            self.f = fd
            magic = self.f.read(4)
        except IOError:
            self.f = fd
            magic = self.f.read(4)
        if magic == "\xa1\xb2\xc3\xd4": #big endian
            self.endian = ">"
        elif  magic == "\xd4\xc3\xb2\xa1": #little endian
            self.endian = "<"
        else:
            raise Scapy_Exception("Not a pcap capture file (bad magic)")
        hdr = self.f.read(20)
        if len(hdr)<20:
            raise Scapy_Exception("Invalid pcap file (too short)")
        vermaj,vermin,tz,sig,snaplen,linktype = struct.unpack(self.endian+"HHIIII",hdr)

        self.linktype = linktype

# class PcapReader(RawPcapReaderFD):
#     def __init__(self, fd):
#         RawPcapReaderFD.__init__(self, fd)
#         try:
#             self.LLcls = conf.l2types[self.linktype]
#         except KeyError:
#             warning("PcapReader: unknown LL type [%i]/[%#x]. Using Raw packets" % (self.linktype,self.linktype))
#             self.LLcls = conf.raw_layer


def dottedQuadToNum(ip):
	"convert decimal dotted quad string to long integer"
	return struct.unpack('>L',socket.inet_aton(ip))[0]

def numToDottedQuad(n):
	"convert long int to dotted quad string"
	return socket.inet_ntoa(struct.pack('>L',n))

Pack_formatstring="dIIHHHHHHHHH"
header='ig_intr_md.ingress_mac_tstamp,hdr.ipv4.src_addr,hdr.ipv4.dst_addr,hdr.ipv4.ttl,hdr.ipv4.protocol,hdr.ipv4.checksum,hdr.tcp.src_port,hdr.tcp.dst_port,hdr.tcp.checksum,hdr.udp.src_port,hdr.udp.dst_port,hdr.udp.checksum,pktSize'
harr=header.split(',')
header_loc_map={harr[i]:i for i in range(len(harr))}

def parse_file_and_append(file_name, task_idx):
    global total_tasks

    local_pkt_list = list()

    # with PcapReader(file_name) as pcap_reader:
    #     maxentries = sum(1 for _ in pcap_reader)

    command = f'capinfos {file_name} | grep "Number of packets" | tr -d " " | grep -oP "Numberofpackets=\K\d+"'
    output = subprocess.check_output(command, shell=True, universal_newlines=True)
    maxentries = int(output.strip())
    tot_pbar = maxentries

    with PcapReader(file_name) as pcap_reader:
        # for j in atpbar(range(tot_pbar), name=f"Task {task_idx}/{total_tasks}"):
        for j in atpbar(range(tot_pbar), name=f"Task {task_idx}/{total_tasks}"):
            if j < maxentries:
                pkt = pcap_reader.read_packet()

                pdict = {}

                pdict['pktSize'] = pkt.wirelen
                pdict['ig_intr_md.ingress_mac_tstamp']=pkt.time
                if pkt.haslayer(IP):
                    pdict['hdr.ipv4.ttl']=pkt[IP].ttl
                    pdict['hdr.ipv4.protocol']=pkt[IP].proto
                    pdict['hdr.ipv4.checksum']=pkt[IP].chksum
                    pdict['hdr.ipv4.src_addr']=pkt[IP].src
                    pdict['hdr.ipv4.dst_addr']=pkt[IP].dst

                if pkt.haslayer(TCP):
                    pdict['hdr.tcp.src_port']=pkt[TCP].sport
                    pdict['hdr.tcp.dst_port']=pkt[TCP].dport
                    pdict['hdr.tcp.checksum']=pkt[TCP].chksum

                if pkt.haslayer(UDP):
                    pdict['hdr.udp.src_port']=pkt[UDP].sport
                    pdict['hdr.udp.dst_port']=pkt[UDP].dport
                    pdict['hdr.udp.checksum']=pkt[UDP].chksum
                def to_list(p):
                    line=[]
                    for h in harr:
                        if (h not in p) or (p[h]==None):
                            line.append(-1)
                        else:
                            line.append(p[h])
                    #timestamp
                    line[0]=np.float128(line[0])
                    #ip
                    if line[1] != -1:
                        line[1] = dottedQuadToNum(line[1])
                    if line[2] != -1:
                        line[2] = dottedQuadToNum(line[2])
                    #everything else
                    for i in range(3,12):
                        line[i] = int(line[i])
                    return line
                local_pkt_list.append(tuple(to_list(pdict)))

    return local_pkt_list


def parse_pcap_into_npy(input_file, count, debug):
    global total_tasks
    m = multiprocessing.Manager()
    file_lock = m.Lock()
    cv = threading.Condition()
    
    final_list = []
    arr = []
    i = 0
    file_list = []

    tmp_dir = tempfile.TemporaryDirectory(dir = "/tmp")
    ret = subprocess.call(f"editcap -c {MAX_FILE_SIZE} {input_file} {tmp_dir.name}/trace.pcap", shell=True)
    for file in os.listdir(tmp_dir.name):
        if file.endswith(".pcap"):
            file_list.append(file)

    file_list.sort()

    total_tasks = int(file_list[-1].split("_")[1])

    print(f"Total number of tasks will be {total_tasks}")

    file_list = [tmp_dir.name + "/" + s for s in file_list]

    # for file_name in file_list:
    #     command = f'capinfos {file_name} | grep "Number of packets" | tr -d " " | grep -oP "Numberofpackets=\K\d+"'
    #     output = subprocess.check_output(command, shell=True, universal_newlines=True)
    #     maxentries = int(output.strip())
    #     print(f"Max entries in {file_name} is {maxentries}")

    task_order_list = list()
    task_idx = 0
    task_order_list.append(task_idx)

    reporter = find_reporter()
    
    with concurrent.futures.ProcessPoolExecutor(max_workers=max(os.cpu_count(), 8), initializer=register_reporter, initargs=[reporter]) as executor:
        for file in file_list:
            task_idx += 1
            future_to_file = {executor.submit(parse_file_and_append, copy.deepcopy(file), copy.deepcopy(task_idx)): file}
        flush()
        for future in concurrent.futures.as_completed(future_to_file):
            file = future_to_file[future]
            try:
                local_pkt_list = future.result()
                for pkt in local_pkt_list:
                    final_list.append(pkt)
            except Exception as exc:
                print('%r generated an exception: %s' % (file, exc))
            else:
                print('%r file contains %d bytes' % (file, len(local_pkt_list)))

    print(f"Parsed {len(final_list)} packets. Allocating numpy ndarray...") 
    arr=np.zeros((len(final_list)),dtype= np.dtype('f16,u4,u4,u2,u2,u2,u2,u2,u2,u2,u2,u2,u4'))

    for i in range(len(final_list)):
        arr[i]=final_list[i]

    tmp_dir.cleanup()

    first_column = arr[:, 0]
    sorted_indices = np.argsort(first_column)
    sorted_array = arr[sorted_indices]

    return sorted_array


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Program used to convert a PCAP into a numpy data structure (easier to work with)')
    parser.add_argument("-i", "--input-file", required=True, type=str, help="Filename for input PCAP")
    parser.add_argument("-o", "--output-file", required=True, type=str, help="Filename for output parsed numpy file (for efficient loading)")
    parser.add_argument("-c", "--count", metavar="count", type=int, default=-1, help="Number of packets to read before stopping. Default is -1 (no limit).")
    parser.add_argument("-v","--verbose", action="store_true", help="Show additional debug info.")

    args = parser.parse_args()

    input_file_path = args.input_file
    output_file_path = args.output_file

    try:
        os.remove(output_file_path)
    except OSError:
        pass

    nparray = parse_pcap_into_npy(input_file_path, args.count, args.verbose)

    np.save(output_file_path, nparray)
    print(f"Output file created: {output_file_path}")
