from bees import profiler as p
from oslo_config import cfg
mks_group = cfg.OptGroup('mks', title='MKS Options', help="\nNova compute node uses WebMKS, a desktop sharing protocol to provide\ninstance console access to VM's created by VMware hypervisors.\n\nRelated options:\nFollowing options must be set to provide console access.\n* mksproxy_base_url\n* enabled\n")
mks_opts = [cfg.URIOpt('mksproxy_base_url', schemes=['http', 'https'], default='http://127.0.0.1:6090/', help='\nLocation of MKS web console proxy\n\nThe URL in the response points to a WebMKS proxy which\nstarts proxying between client and corresponding vCenter\nserver where instance runs. In order to use the web based\nconsole access, WebMKS proxy should be installed and configured\n\nPossible values:\n\n* Must be a valid URL of the form:``http://host:port/`` or\n  ``https://host:port/``\n'), cfg.BoolOpt('enabled', default=False, help='\nEnables graphical console access for virtual machines.\n')]

@p.trace('register_opts')
def register_opts(conf):
    conf.register_group(mks_group)
    conf.register_opts(mks_opts, group=mks_group)

@p.trace('list_opts')
def list_opts():
    return {mks_group: mks_opts}