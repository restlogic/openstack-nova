from bees import profiler as p
'Serial consoles module.'
import socket
from oslo_log import log as logging
import nova.conf
from nova import exception
from nova import utils
LOG = logging.getLogger(__name__)
ALLOCATED_PORTS = set()
SERIAL_LOCK = 'serial-lock'
CONF = nova.conf.CONF

@p.trace('acquire_port')
@utils.synchronized(SERIAL_LOCK)
def acquire_port(host):
    """Returns a free TCP port on host.

    Find and returns a free TCP port on 'host' in the range
    of 'CONF.serial_console.port_range'.
    """
    (start, stop) = _get_port_range()
    for port in range(start, stop):
        if (host, port) in ALLOCATED_PORTS:
            continue
        try:
            _verify_port(host, port)
            ALLOCATED_PORTS.add((host, port))
            return port
        except exception.SocketPortInUseException as e:
            LOG.warning(e.format_message())
    raise exception.SocketPortRangeExhaustedException(host=host)

@p.trace('release_port')
@utils.synchronized(SERIAL_LOCK)
def release_port(host, port):
    """Release TCP port to be used next time."""
    ALLOCATED_PORTS.discard((host, port))

@p.trace('_get_port_range')
def _get_port_range():
    config_range = CONF.serial_console.port_range
    (start, stop) = map(int, config_range.split(':'))
    if start >= stop:
        default_port_range = nova.conf.serial_console.DEFAULT_PORT_RANGE
        LOG.warning('serial_console.port_range should be in the format <start>:<stop> and start < stop, Given value %(port_range)s is invalid. Taking the default port range %(default)s.', {'port_range': config_range, 'default': default_port_range})
        (start, stop) = map(int, default_port_range.split(':'))
    return (start, stop)

@p.trace('_verify_port')
def _verify_port(host, port):
    s = socket.socket()
    try:
        s.bind((host, port))
    except socket.error as e:
        raise exception.SocketPortInUseException(host=host, port=port, error=e)
    finally:
        s.close()