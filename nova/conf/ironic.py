from bees import profiler as p
from keystoneauth1 import loading as ks_loading
from oslo_config import cfg
from nova.conf import utils as confutils
DEFAULT_SERVICE_TYPE = 'baremetal'
ironic_group = cfg.OptGroup('ironic', title='Ironic Options', help='\nConfiguration options for Ironic driver (Bare Metal).\nIf using the Ironic driver following options must be set:\n* auth_type\n* auth_url\n* project_name\n* username\n* password\n* project_domain_id or project_domain_name\n* user_domain_id or user_domain_name\n')
ironic_options = [cfg.IntOpt('api_max_retries', default=60, min=0, help='\nThe number of times to retry when a request conflicts.\nIf set to 0, only try once, no retries.\n\nRelated options:\n\n* api_retry_interval\n'), cfg.IntOpt('api_retry_interval', default=2, min=0, help='\nThe number of seconds to wait before retrying the request.\n\nRelated options:\n\n* api_max_retries\n'), cfg.IntOpt('serial_console_state_timeout', default=10, min=0, help='Timeout (seconds) to wait for node serial console state changed. Set to 0 to disable timeout.'), cfg.StrOpt('partition_key', default=None, mutable=True, max_length=255, regex='^[a-zA-Z0-9_.-]*$', help='Case-insensitive key to limit the set of nodes that may be managed by this service to the set of nodes in Ironic which have a matching conductor_group property. If unset, all available nodes will be eligible to be managed by this service. Note that setting this to the empty string (``""``) will match the default conductor group, and is different than leaving the option unset.'), cfg.ListOpt('peer_list', default=[], mutable=True, help='List of hostnames for all nova-compute services (including this host) with this partition_key config value. Nodes matching the partition_key value will be distributed between all services specified here. If partition_key is unset, this option is ignored.')]

@p.trace('register_opts')
def register_opts(conf):
    conf.register_group(ironic_group)
    conf.register_opts(ironic_options, group=ironic_group)
    confutils.register_ksa_opts(conf, ironic_group, DEFAULT_SERVICE_TYPE)

@p.trace('list_opts')
def list_opts():
    return {ironic_group: ironic_options + ks_loading.get_session_conf_options() + ks_loading.get_auth_common_conf_options() + ks_loading.get_auth_plugin_conf_options('v3password') + confutils.get_ksa_adapter_opts(DEFAULT_SERVICE_TYPE)}