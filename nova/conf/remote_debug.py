from bees import profiler as p
from oslo_config import cfg
debugger_group = cfg.OptGroup('remote_debug', title='debugger options')
CLI_OPTS = [cfg.HostAddressOpt('host', help='\nDebug host (IP or name) to connect to.\n\nThis command line parameter is used when you want to connect to a nova service\nvia a debugger running on a different host.\n\nNote that using the remote debug option changes how nova uses the eventlet\nlibrary to support async IO. This could result in failures that do not occur\nunder normal operation. Use at your own risk.\n\nPossible Values:\n\n* IP address of a remote host as a command line parameter to a nova service.\n  For example::\n\n    nova-compute --config-file /etc/nova/nova.conf       --remote_debug-host <IP address of the debugger>\n'), cfg.PortOpt('port', help='\nDebug port to connect to.\n\nThis command line parameter allows you to specify the port you want to use to\nconnect to a nova service via a debugger running on different host.\n\nNote that using the remote debug option changes how nova uses the eventlet\nlibrary to support async IO. This could result in failures that do not occur\nunder normal operation. Use at your own risk.\n\nPossible Values:\n\n* Port number you want to use as a command line parameter to a nova service.\n  For example::\n\n    nova-compute --config-file /etc/nova/nova.conf       --remote_debug-host <IP address of the debugger>       --remote_debug-port <port debugger is listening on>.\n')]

@p.trace('register_cli_opts')
def register_cli_opts(conf):
    conf.register_group(debugger_group)
    conf.register_cli_opts(CLI_OPTS, group=debugger_group)

@p.trace('list_opts')
def list_opts():
    return {debugger_group: CLI_OPTS}