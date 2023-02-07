from bees import profiler as p
from oslo_config import cfg
from nova.conf import paths
zvm_opt_group = cfg.OptGroup('zvm', title='zVM Options', help='\nzvm options allows cloud administrator to configure related\nz/VM hypervisor driver to be used within an OpenStack deployment.\n\nzVM options are used when the compute_driver is set to use\nzVM (compute_driver=zvm.ZVMDriver)\n')
zvm_opts = [cfg.URIOpt('cloud_connector_url', sample_default='http://zvm.example.org:8080/', help='\nURL to be used to communicate with z/VM Cloud Connector.\n'), cfg.StrOpt('ca_file', default=None, help='\nCA certificate file to be verified in httpd server with TLS enabled\n\nA string, it must be a path to a CA bundle to use.\n'), cfg.StrOpt('image_tmp_path', default=paths.state_path_def('images'), sample_default='$state_path/images', help='\nThe path at which images will be stored (snapshot, deploy, etc).\n\nImages used for deploy and images captured via snapshot\nneed to be stored on the local disk of the compute host.\nThis configuration identifies the directory location.\n\nPossible values:\n    A file system path on the host running the compute service.\n'), cfg.IntOpt('reachable_timeout', default=300, help='\nTimeout (seconds) to wait for an instance to start.\n\nThe z/VM driver relies on communication between the instance and cloud\nconnector. After an instance is created, it must have enough time to wait\nfor all the network info to be written into the user directory.\nThe driver will keep rechecking network status to the instance with the\ntimeout value, If setting network failed, it will notify the user that\nstarting the instance failed and put the instance in ERROR state.\nThe underlying z/VM guest will then be deleted.\n\nPossible Values:\n    Any positive integer. Recommended to be at least 300 seconds (5 minutes),\n    but it will vary depending on instance and system load.\n    A value of 0 is used for debug. In this case the underlying z/VM guest\n    will not be deleted when the instance is marked in ERROR state.\n')]

@p.trace('register_opts')
def register_opts(conf):
    conf.register_group(zvm_opt_group)
    conf.register_opts(zvm_opts, group=zvm_opt_group)

@p.trace('list_opts')
def list_opts():
    return {zvm_opt_group: zvm_opts}