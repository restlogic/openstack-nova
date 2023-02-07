from bees import profiler as p
from oslo_config import cfg
conductor_group = cfg.OptGroup('conductor', title='Conductor Options', help="\nOptions under this group are used to define Conductor's communication,\nwhich manager should be act as a proxy between computes and database,\nand finally, how many worker processes will be used.\n")
ALL_OPTS = [cfg.IntOpt('workers', help='\nNumber of workers for OpenStack Conductor service. The default will be the\nnumber of CPUs available.\n')]
migrate_opts = [cfg.IntOpt('migrate_max_retries', default=-1, min=-1, help='\nNumber of times to retry live-migration before failing.\n\nPossible values:\n\n* If == -1, try until out of hosts (default)\n* If == 0, only try once, no retries\n* Integer greater than 0\n')]

@p.trace('register_opts')
def register_opts(conf):
    conf.register_group(conductor_group)
    conf.register_opts(ALL_OPTS, group=conductor_group)
    conf.register_opts(migrate_opts)

@p.trace('list_opts')
def list_opts():
    return {'DEFAULT': migrate_opts, conductor_group: ALL_OPTS}