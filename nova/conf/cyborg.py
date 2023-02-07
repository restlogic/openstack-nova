from bees import profiler as p
from keystoneauth1 import loading as ks_loading
from oslo_config import cfg
from nova.conf import utils as confutils
DEFAULT_SERVICE_TYPE = 'accelerator'
CYBORG_GROUP = 'cyborg'
cyborg_group = cfg.OptGroup(CYBORG_GROUP, title='Cyborg Options', help='\nConfiguration options for Cyborg (accelerator as a service).\n')

@p.trace('register_opts')
def register_opts(conf):
    conf.register_group(cyborg_group)
    confutils.register_ksa_opts(conf, cyborg_group, DEFAULT_SERVICE_TYPE, include_auth=False)

@p.trace('list_opts')
def list_opts():
    return {cyborg_group: ks_loading.get_session_conf_options() + confutils.get_ksa_adapter_opts(DEFAULT_SERVICE_TYPE)}