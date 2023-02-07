from bees import profiler as p
'\nLinux network specific helpers.\n'
import os
from oslo_concurrency import processutils
from oslo_log import log as logging
from oslo_utils import excutils
import nova.privsep.linux_net
LOG = logging.getLogger(__name__)

@p.trace('device_exists')
def device_exists(device):
    """Check if ethernet device exists."""
    return os.path.exists('/sys/class/net/%s' % device)

@p.trace('delete_net_dev')
def delete_net_dev(dev):
    """Delete a network device only if it exists."""
    if device_exists(dev):
        try:
            delete_net_dev_escalated(dev)
            LOG.debug("Net device removed: '%s'", dev)
        except processutils.ProcessExecutionError:
            with excutils.save_and_reraise_exception():
                LOG.error("Failed removing net device: '%s'", dev)

@p.trace('delete_net_dev_escalated')
@nova.privsep.sys_admin_pctxt.entrypoint
def delete_net_dev_escalated(dev):
    processutils.execute('ip', 'link', 'delete', dev, check_exit_code=[0, 2, 254])

@p.trace('set_device_mtu')
@nova.privsep.sys_admin_pctxt.entrypoint
def set_device_mtu(dev, mtu):
    if mtu:
        processutils.execute('ip', 'link', 'set', dev, 'mtu', mtu, check_exit_code=[0, 2, 254])

@p.trace('set_device_enabled')
@nova.privsep.sys_admin_pctxt.entrypoint
def set_device_enabled(dev):
    _set_device_enabled_inner(dev)

@p.trace('_set_device_enabled_inner')
def _set_device_enabled_inner(dev):
    processutils.execute('ip', 'link', 'set', dev, 'up', check_exit_code=[0, 2, 254])

@p.trace('set_device_trust')
@nova.privsep.sys_admin_pctxt.entrypoint
def set_device_trust(dev, vf_num, trusted):
    _set_device_trust_inner(dev, vf_num, trusted)

@p.trace('_set_device_trust_inner')
def _set_device_trust_inner(dev, vf_num, trusted):
    processutils.execute('ip', 'link', 'set', dev, 'vf', vf_num, 'trust', bool(trusted) and 'on' or 'off', check_exit_code=[0, 2, 254])

@p.trace('set_device_macaddr')
@nova.privsep.sys_admin_pctxt.entrypoint
def set_device_macaddr(dev, mac_addr, port_state=None):
    _set_device_macaddr_inner(dev, mac_addr, port_state=port_state)

@p.trace('_set_device_macaddr_inner')
def _set_device_macaddr_inner(dev, mac_addr, port_state=None):
    if port_state:
        processutils.execute('ip', 'link', 'set', dev, 'address', mac_addr, port_state, check_exit_code=[0, 2, 254])
    else:
        processutils.execute('ip', 'link', 'set', dev, 'address', mac_addr, check_exit_code=[0, 2, 254])

@p.trace('set_device_macaddr_and_vlan')
@nova.privsep.sys_admin_pctxt.entrypoint
def set_device_macaddr_and_vlan(dev, vf_num, mac_addr, vlan):
    processutils.execute('ip', 'link', 'set', dev, 'vf', vf_num, 'mac', mac_addr, 'vlan', vlan, run_as_root=True, check_exit_code=[0, 2, 254])

@p.trace('create_tap_dev')
@nova.privsep.sys_admin_pctxt.entrypoint
def create_tap_dev(dev, mac_address=None, multiqueue=False):
    if not device_exists(dev):
        try:
            cmd = ('ip', 'tuntap', 'add', dev, 'mode', 'tap')
            if multiqueue:
                cmd = cmd + ('multi_queue',)
            processutils.execute(*cmd, check_exit_code=[0, 2, 254])
        except processutils.ProcessExecutionError:
            if multiqueue:
                LOG.warning('Failed to create a tap device with ip tuntap. tunctl does not support creation of multi-queue enabled devices, skipping fallback.')
                raise
            processutils.execute('tunctl', '-b', '-t', dev)
        if mac_address:
            _set_device_macaddr_inner(dev, mac_address)
        _set_device_enabled_inner(dev)

@p.trace('add_vlan')
@nova.privsep.sys_admin_pctxt.entrypoint
def add_vlan(bridge_interface, interface, vlan_num):
    processutils.execute('ip', 'link', 'add', 'link', bridge_interface, 'name', interface, 'type', 'vlan', 'id', vlan_num, check_exit_code=[0, 2, 254])