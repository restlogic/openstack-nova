from bees import profiler as p
from keystoneauth1 import loading as ks_loading
from oslo_config import cfg
from nova.conf import utils as confutils
DEFAULT_SERVICE_TYPE = 'network'
NEUTRON_GROUP = 'neutron'
neutron_group = cfg.OptGroup(NEUTRON_GROUP, title='Neutron Options', help='\nConfiguration options for neutron (network connectivity as a service).\n')
neutron_opts = [cfg.StrOpt('ovs_bridge', default='br-int', help='\nDefault name for the Open vSwitch integration bridge.\n\nSpecifies the name of an integration bridge interface used by OpenvSwitch.\nThis option is only used if Neutron does not specify the OVS bridge name in\nport binding responses.\n'), cfg.StrOpt('default_floating_pool', default='nova', help='\nDefault name for the floating IP pool.\n\nSpecifies the name of floating IP pool used for allocating floating IPs. This\noption is only used if Neutron does not specify the floating IP pool name in\nport binding reponses.\n'), cfg.IntOpt('extension_sync_interval', default=600, min=0, help='\nInteger value representing the number of seconds to wait before querying\nNeutron for extensions.  After this number of seconds the next time Nova\nneeds to create a resource in Neutron it will requery Neutron for the\nextensions that it has loaded.  Setting value to 0 will refresh the\nextensions with no wait.\n'), cfg.ListOpt('physnets', default=[], help='\nList of physnets present on this host.\n\nFor each *physnet* listed, an additional section,\n``[neutron_physnet_$PHYSNET]``, will be added to the configuration file. Each\nsection must be configured with a single configuration option, ``numa_nodes``,\nwhich should be a list of node IDs for all NUMA nodes this physnet is\nassociated with. For example::\n\n    [neutron]\n    physnets = foo, bar\n\n    [neutron_physnet_foo]\n    numa_nodes = 0\n\n    [neutron_physnet_bar]\n    numa_nodes = 0,1\n\nAny *physnet* that is not listed using this option will be treated as having no\nparticular NUMA node affinity.\n\nTunnelled networks (VXLAN, GRE, ...) cannot be accounted for in this way and\nare instead configured using the ``[neutron_tunnel]`` group. For example::\n\n    [neutron_tunnel]\n    numa_nodes = 1\n\nRelated options:\n\n* ``[neutron_tunnel] numa_nodes`` can be used to configure NUMA affinity for\n  all tunneled networks\n* ``[neutron_physnet_$PHYSNET] numa_nodes`` must be configured for each value\n  of ``$PHYSNET`` specified by this option\n'), cfg.IntOpt('http_retries', default=3, min=0, help='\nNumber of times neutronclient should retry on any failed http call.\n\n0 means connection is attempted only once. Setting it to any positive integer\nmeans that on failure connection is retried that many times e.g. setting it\nto 3 means total attempts to connect will be 4.\n\nPossible values:\n\n* Any integer value. 0 means connection is attempted only once\n')]
metadata_proxy_opts = [cfg.BoolOpt('service_metadata_proxy', default=False, help="\nWhen set to True, this option indicates that Neutron will be used to proxy\nmetadata requests and resolve instance ids. Otherwise, the instance ID must be\npassed to the metadata request in the 'X-Instance-ID' header.\n\nRelated options:\n\n* metadata_proxy_shared_secret\n"), cfg.StrOpt('metadata_proxy_shared_secret', default='', secret=True, help="\nThis option holds the shared secret string used to validate proxy requests to\nNeutron metadata requests. In order to be used, the\n'X-Metadata-Provider-Signature' header must be supplied in the request.\n\nRelated options:\n\n* service_metadata_proxy\n")]
ALL_OPTS = neutron_opts + metadata_proxy_opts

@p.trace('register_opts')
def register_opts(conf):
    conf.register_group(neutron_group)
    conf.register_opts(ALL_OPTS, group=neutron_group)
    confutils.register_ksa_opts(conf, neutron_group, DEFAULT_SERVICE_TYPE)

@p.trace('register_dynamic_opts')
def register_dynamic_opts(conf):
    """Register dynamically-generated options and groups.

    This must be called by the service that wishes to use the options **after**
    the initial configuration has been loaded.
    """
    opt = cfg.ListOpt('numa_nodes', default=[], item_type=cfg.types.Integer())
    conf.register_opt(opt, group='neutron_tunnel')
    for physnet in conf.neutron.physnets:
        conf.register_opt(opt, group='neutron_physnet_%s' % physnet)

@p.trace('list_opts')
def list_opts():
    return {neutron_group: ALL_OPTS + ks_loading.get_session_conf_options() + ks_loading.get_auth_common_conf_options() + ks_loading.get_auth_plugin_conf_options('password') + ks_loading.get_auth_plugin_conf_options('v2password') + ks_loading.get_auth_plugin_conf_options('v3password') + confutils.get_ksa_adapter_opts(DEFAULT_SERVICE_TYPE)}