from bees import profiler as p
from keystoneauth1 import loading as ks_loading
from oslo_config import cfg
from nova.conf import utils as confutils
DEFAULT_SERVICE_TYPE = 'placement'
placement_group = cfg.OptGroup('placement', title='Placement Service Options', help='Configuration options for connecting to the placement API service')

@p.trace('register_opts')
def register_opts(conf):
    conf.register_group(placement_group)
    confutils.register_ksa_opts(conf, placement_group, DEFAULT_SERVICE_TYPE)

@p.trace('list_opts')
def list_opts():
    return {placement_group.name: ks_loading.get_session_conf_options() + ks_loading.get_auth_common_conf_options() + ks_loading.get_auth_plugin_conf_options('password') + ks_loading.get_auth_plugin_conf_options('v2password') + ks_loading.get_auth_plugin_conf_options('v3password') + confutils.get_ksa_adapter_opts(DEFAULT_SERVICE_TYPE)}