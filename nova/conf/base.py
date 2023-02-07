from bees import profiler as p
from oslo_config import cfg
base_options = [cfg.IntOpt('password_length', default=12, min=0, help='Length of generated instance admin passwords.'), cfg.StrOpt('instance_usage_audit_period', default='month', regex='^(hour|month|day|year)(@([0-9]+))?$', help='\nTime period to generate instance usages for. It is possible to define optional\noffset to given period by appending @ character followed by a number defining\noffset.\n\nPossible values:\n\n*  period, example: ``hour``, ``day``, ``month` or ``year``\n*  period with offset, example: ``month@15`` will result in monthly audits\n   starting on 15th day of month.\n'), cfg.BoolOpt('use_rootwrap_daemon', default=False, help='\nStart and use a daemon that can run the commands that need to be run with\nroot privileges. This option is usually enabled on nodes that run nova compute\nprocesses.\n'), cfg.StrOpt('rootwrap_config', default='/etc/nova/rootwrap.conf', help='\nPath to the rootwrap configuration file.\n\nGoal of the root wrapper is to allow a service-specific unprivileged user to\nrun a number of actions as the root user in the safest manner possible.\nThe configuration file used here must match the one defined in the sudoers\nentry.\n'), cfg.StrOpt('tempdir', help='Explicitly specify the temporary working directory.')]

@p.trace('register_opts')
def register_opts(conf):
    conf.register_opts(base_options)

@p.trace('list_opts')
def list_opts():
    return {'DEFAULT': base_options}