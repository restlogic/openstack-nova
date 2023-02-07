from bees import profiler as p
from keystoneauth1 import loading as ks_loading
from oslo_config import cfg
vendordata_group = cfg.OptGroup('vendordata_dynamic_auth', title='Vendordata dynamic fetch auth options', help='\nOptions within this group control the authentication of the vendordata\nsubsystem of the metadata API server (and config drive) with external systems.\n')

@p.trace('register_opts')
def register_opts(conf):
    conf.register_group(vendordata_group)
    ks_loading.register_session_conf_options(conf, vendordata_group.name)
    ks_loading.register_auth_conf_options(conf, vendordata_group.name)

@p.trace('list_opts')
def list_opts():
    return {vendordata_group: ks_loading.get_session_conf_options() + ks_loading.get_auth_common_conf_options() + ks_loading.get_auth_plugin_conf_options('password') + ks_loading.get_auth_plugin_conf_options('v2password') + ks_loading.get_auth_plugin_conf_options('v3password')}