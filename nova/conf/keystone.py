from bees import profiler as p
from keystoneauth1 import loading as ks_loading
from oslo_config import cfg
from nova.conf import utils as confutils
DEFAULT_SERVICE_TYPE = 'identity'
keystone_group = cfg.OptGroup('keystone', title='Keystone Options', help='Configuration options for the identity service')

@p.trace('register_opts')
def register_opts(conf):
    conf.register_group(keystone_group)
    confutils.register_ksa_opts(conf, keystone_group.name, DEFAULT_SERVICE_TYPE, include_auth=False)

@p.trace('list_opts')
def list_opts():
    return {keystone_group: ks_loading.get_session_conf_options() + confutils.get_ksa_adapter_opts(DEFAULT_SERVICE_TYPE)}