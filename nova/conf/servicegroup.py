from bees import profiler as p
from oslo_config import cfg
SERVICEGROUP_OPTS = [cfg.StrOpt('servicegroup_driver', default='db', choices=[('db', 'Database ServiceGroup driver'), ('mc', 'Memcache ServiceGroup driver')], help='\nThis option specifies the driver to be used for the servicegroup service.\n\nServiceGroup API in nova enables checking status of a compute node. When a\ncompute worker running the nova-compute daemon starts, it calls the join API\nto join the compute group. Services like nova scheduler can query the\nServiceGroup API to check if a node is alive. Internally, the ServiceGroup\nclient driver automatically updates the compute worker status. There are\nmultiple backend implementations for this service: Database ServiceGroup driver\nand Memcache ServiceGroup driver.\n\nRelated Options:\n\n* ``service_down_time`` (maximum time since last check-in for up service)\n')]

@p.trace('register_opts')
def register_opts(conf):
    conf.register_opts(SERVICEGROUP_OPTS)

@p.trace('list_opts')
def list_opts():
    return {'DEFAULT': SERVICEGROUP_OPTS}