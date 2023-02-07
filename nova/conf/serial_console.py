from bees import profiler as p
from oslo_config import cfg
DEFAULT_PORT_RANGE = '10000:20000'
serial_opt_group = cfg.OptGroup('serial_console', title='The serial console feature', help='\nThe serial console feature allows you to connect to a guest in case a\ngraphical console like VNC, RDP or SPICE is not available. This is only\ncurrently supported for the libvirt, Ironic and hyper-v drivers.')
ALL_OPTS = [cfg.BoolOpt('enabled', default=False, help='\nEnable the serial console feature.\n\nIn order to use this feature, the service ``nova-serialproxy`` needs to run.\nThis service is typically executed on the controller node.\n'), cfg.StrOpt('port_range', default=DEFAULT_PORT_RANGE, regex='^\\d+:\\d+$', help="\nA range of TCP ports a guest can use for its backend.\n\nEach instance which gets created will use one port out of this range. If the\nrange is not big enough to provide another port for an new instance, this\ninstance won't get launched.\n\nPossible values:\n\n* Each string which passes the regex ``^\\d+:\\d+$`` For example\n  ``10000:20000``. Be sure that the first port number is lower than the second\n  port number and that both are in range from 0 to 65535.\n"), cfg.URIOpt('base_url', default='ws://127.0.0.1:6083/', help='\nThe URL an end user would use to connect to the ``nova-serialproxy`` service.\n\nThe ``nova-serialproxy`` service is called with this token enriched URL\nand establishes the connection to the proper instance.\n\nRelated options:\n\n* The IP address must be identical to the address to which the\n  ``nova-serialproxy`` service is listening (see option ``serialproxy_host``\n  in this section).\n* The port must be the same as in the option ``serialproxy_port`` of this\n  section.\n* If you choose to use a secured websocket connection, then start this option\n  with ``wss://`` instead of the unsecured ``ws://``. The options ``cert``\n  and ``key`` in the ``[DEFAULT]`` section have to be set for that.\n'), cfg.StrOpt('proxyclient_address', default='127.0.0.1', help='\nThe IP address to which proxy clients (like ``nova-serialproxy``) should\nconnect to get the serial console of an instance.\n\nThis is typically the IP address of the host of a ``nova-compute`` service.\n')]
CLI_OPTS = [cfg.StrOpt('serialproxy_host', default='0.0.0.0', help='\nThe IP address which is used by the ``nova-serialproxy`` service to listen\nfor incoming requests.\n\nThe ``nova-serialproxy`` service listens on this IP address for incoming\nconnection requests to instances which expose serial console.\n\nRelated options:\n\n* Ensure that this is the same IP address which is defined in the option\n  ``base_url`` of this section or use ``0.0.0.0`` to listen on all addresses.\n'), cfg.PortOpt('serialproxy_port', default=6083, help='\nThe port number which is used by the ``nova-serialproxy`` service to listen\nfor incoming requests.\n\nThe ``nova-serialproxy`` service listens on this port number for incoming\nconnection requests to instances which expose serial console.\n\nRelated options:\n\n* Ensure that this is the same port number which is defined in the option\n  ``base_url`` of this section.\n')]
ALL_OPTS.extend(CLI_OPTS)

@p.trace('register_opts')
def register_opts(conf):
    conf.register_group(serial_opt_group)
    conf.register_opts(ALL_OPTS, group=serial_opt_group)

@p.trace('register_cli_opts')
def register_cli_opts(conf):
    conf.register_group(serial_opt_group)
    conf.register_cli_opts(CLI_OPTS, serial_opt_group)

@p.trace('list_opts')
def list_opts():
    return {serial_opt_group: ALL_OPTS}