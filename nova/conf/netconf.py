from bees import profiler as p
import socket
from oslo_config import cfg
from oslo_utils import netutils
netconf_opts = [cfg.StrOpt('my_ip', default=netutils.get_my_ipv4(), sample_default='<host_ipv4>', help='\nThe IP address which the host is using to connect to the management network.\n\nPossible values:\n\n* String with valid IP address. Default is IPv4 address of this host.\n\nRelated options:\n\n* my_block_storage_ip\n'), cfg.StrOpt('my_block_storage_ip', default='$my_ip', help='\nThe IP address which is used to connect to the block storage network.\n\nPossible values:\n\n* String with valid IP address. Default is IP address of this host.\n\nRelated options:\n\n* my_ip - if my_block_storage_ip is not set, then my_ip value is used.\n'), cfg.StrOpt('host', default=socket.gethostname(), sample_default='<current_hostname>', help='\nHostname, FQDN or IP address of this host.\n\nUsed as:\n\n* the oslo.messaging queue name for nova-compute worker\n* we use this value for the binding_host sent to neutron. This means if you use\n  a neutron agent, it should have the same value for host.\n* cinder host attachment information\n\nMust be valid within AMQP key.\n\nPossible values:\n\n* String with hostname, FQDN or IP address. Default is hostname of this host.\n'), cfg.BoolOpt('flat_injected', default=False, help='\nThis option determines whether the network setup information is injected into\nthe VM before it is booted. While it was originally designed to be used only\nby nova-network, it is also used by the vmware virt driver to control whether\nnetwork information is injected into a VM. The libvirt virt driver also uses it\nwhen we use config_drive to configure network to control whether network\ninformation is injected into a VM.\n')]

@p.trace('register_opts')
def register_opts(conf):
    conf.register_opts(netconf_opts)

@p.trace('list_opts')
def list_opts():
    return {'DEFAULT': netconf_opts}