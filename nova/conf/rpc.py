from bees import profiler as p
from oslo_config import cfg
rpc_opts = [cfg.IntOpt('long_rpc_timeout', default=1800, help='\nThis option allows setting an alternate timeout value for RPC calls\nthat have the potential to take a long time. If set, RPC calls to\nother services will use this value for the timeout (in seconds)\ninstead of the global rpc_response_timeout value.\n\nOperations with RPC calls that utilize this value:\n\n* live migration\n* scheduling\n* enabling/disabling a compute service\n* image pre-caching\n* snapshot-based / cross-cell resize\n* resize / cold migration\n* volume attach\n\nRelated options:\n\n* rpc_response_timeout\n')]
ALL_OPTS = rpc_opts

@p.trace('register_opts')
def register_opts(conf):
    conf.register_opts(ALL_OPTS)

@p.trace('list_opts')
def list_opts():
    return {'DEFAULT': ALL_OPTS}