from bees import profiler as p
'\nlibvirt specific routines.\n'
import binascii
import os
import stat
from oslo_concurrency import processutils
from oslo_log import log as logging
from oslo_utils import units
from oslo_utils import uuidutils
import nova.privsep
LOG = logging.getLogger(__name__)

@p.trace('dmcrypt_create_volume')
@nova.privsep.sys_admin_pctxt.entrypoint
def dmcrypt_create_volume(target, device, cipher, key_size, key):
    """Sets up a dmcrypt mapping

    :param target: device mapper logical device name
    :param device: underlying block device
    :param cipher: encryption cipher string digestible by cryptsetup
    :param key_size: encryption key size
    :param key: encoded encryption key bytestring
    """
    cmd = ('cryptsetup', 'create', target, device, '--cipher=' + cipher, '--key-size=' + str(key_size), '--key-file=-')
    key = binascii.hexlify(key).decode('utf-8')
    processutils.execute(*cmd, process_input=key)

@p.trace('dmcrypt_delete_volume')
@nova.privsep.sys_admin_pctxt.entrypoint
def dmcrypt_delete_volume(target):
    """Deletes a dmcrypt mapping

    :param target: name of the mapped logical device
    """
    processutils.execute('cryptsetup', 'remove', target)

@p.trace('ploop_init')
@nova.privsep.sys_admin_pctxt.entrypoint
def ploop_init(size, disk_format, fs_type, disk_path):
    """Initialize ploop disk, make it readable for non-root user

    :param disk_format: data allocation format (raw or expanded)
    :param fs_type: filesystem (ext4, ext3, none)
    :param disk_path: ploop image file
    """
    processutils.execute('ploop', 'init', '-s', size, '-f', disk_format, '-t', fs_type, disk_path, check_exit_code=True)
    st = os.stat(disk_path)
    os.chmod(disk_path, st.st_mode | stat.S_IROTH)

@p.trace('ploop_resize')
@nova.privsep.sys_admin_pctxt.entrypoint
def ploop_resize(disk_path, size):
    """Resize ploop disk

    :param disk_path: ploop image file
    :param size: new size (in bytes)
    """
    processutils.execute('prl_disk_tool', 'resize', '--size', '%dM' % (size // units.Mi), '--resize_partition', '--hdd', disk_path, check_exit_code=True)

@p.trace('ploop_restore_descriptor')
@nova.privsep.sys_admin_pctxt.entrypoint
def ploop_restore_descriptor(image_dir, base_delta, fmt):
    """Restore ploop disk descriptor XML

    :param image_dir: path to where descriptor XML is created
    :param base_delta: ploop image file containing the data
    :param fmt: ploop data allocation format (raw or expanded)
    """
    processutils.execute('ploop', 'restore-descriptor', '-f', fmt, image_dir, base_delta, check_exit_code=True)

@p.trace('plug_infiniband_vif')
@nova.privsep.sys_admin_pctxt.entrypoint
def plug_infiniband_vif(vnic_mac, device_id, fabric, net_model, pci_slot):
    processutils.execute('ebrctl', 'add-port', vnic_mac, device_id, fabric, net_model, pci_slot)

@p.trace('unplug_infiniband_vif')
@nova.privsep.sys_admin_pctxt.entrypoint
def unplug_infiniband_vif(fabric, vnic_mac):
    processutils.execute('ebrctl', 'del-port', fabric, vnic_mac)

@p.trace('plug_midonet_vif')
@nova.privsep.sys_admin_pctxt.entrypoint
def plug_midonet_vif(port_id, dev):
    processutils.execute('mm-ctl', '--bind-port', port_id, dev)

@p.trace('unplug_midonet_vif')
@nova.privsep.sys_admin_pctxt.entrypoint
def unplug_midonet_vif(port_id):
    processutils.execute('mm-ctl', '--unbind-port', port_id)

@p.trace('plug_plumgrid_vif')
@nova.privsep.sys_admin_pctxt.entrypoint
def plug_plumgrid_vif(dev, iface_id, vif_address, net_id, tenant_id):
    processutils.execute('ifc_ctl', 'gateway', 'add_port', dev)
    processutils.execute('ifc_ctl', 'gateway', 'ifup', dev, 'access_vm', iface_id, vif_address, 'pgtag2=%s' % net_id, 'pgtag1=%s' % tenant_id)

@p.trace('unplug_plumgrid_vif')
@nova.privsep.sys_admin_pctxt.entrypoint
def unplug_plumgrid_vif(dev):
    processutils.execute('ifc_ctl', 'gateway', 'ifdown', dev)
    processutils.execute('ifc_ctl', 'gateway', 'del_port', dev)

@p.trace('readpty')
@nova.privsep.sys_admin_pctxt.entrypoint
def readpty(path):
    import fcntl
    try:
        with open(path, 'r') as f:
            current_flags = fcntl.fcntl(f.fileno(), fcntl.F_GETFL)
            fcntl.fcntl(f.fileno(), fcntl.F_SETFL, current_flags | os.O_NONBLOCK)
            return f.read()
    except Exception as exc:
        LOG.info('Ignored error while reading from instance console pty: %s', exc)
        return ''

@p.trace('create_mdev')
@nova.privsep.sys_admin_pctxt.entrypoint
def create_mdev(physical_device, mdev_type, uuid=None):
    """Instantiate a mediated device."""
    if uuid is None:
        uuid = uuidutils.generate_uuid()
    fpath = '/sys/class/mdev_bus/{0}/mdev_supported_types/{1}/create'
    fpath = fpath.format(physical_device, mdev_type)
    with open(fpath, 'w') as f:
        f.write(uuid)
    return uuid

@p.trace('systemd_run_qb_mount')
@nova.privsep.sys_admin_pctxt.entrypoint
def systemd_run_qb_mount(qb_vol, mnt_base, cfg_file=None):
    """Mount QB volume in separate CGROUP"""
    sysdr_cmd = ['systemd-run', '--scope', 'mount.quobyte', '--disable-xattrs', qb_vol, mnt_base]
    if cfg_file:
        sysdr_cmd.extend(['-c', cfg_file])
    return processutils.execute(*sysdr_cmd)

@p.trace('unprivileged_qb_mount')
def unprivileged_qb_mount(qb_vol, mnt_base, cfg_file=None):
    """Mount QB volume"""
    mnt_cmd = ['mount.quobyte', '--disable-xattrs', qb_vol, mnt_base]
    if cfg_file:
        mnt_cmd.extend(['-c', cfg_file])
    return processutils.execute(*mnt_cmd)

@p.trace('umount')
@nova.privsep.sys_admin_pctxt.entrypoint
def umount(mnt_base):
    """Unmount volume"""
    unprivileged_umount(mnt_base)

@p.trace('unprivileged_umount')
def unprivileged_umount(mnt_base):
    """Unmount volume"""
    umnt_cmd = ['umount', mnt_base]
    return processutils.execute(*umnt_cmd)

@p.trace('get_pmem_namespaces')
@nova.privsep.sys_admin_pctxt.entrypoint
def get_pmem_namespaces():
    ndctl_cmd = ['ndctl', 'list', '-X']
    nss_info = processutils.execute(*ndctl_cmd)[0]
    return nss_info

@p.trace('cleanup_vpmem')
@nova.privsep.sys_admin_pctxt.entrypoint
def cleanup_vpmem(devpath):
    daxio_cmd = ['daxio', '-z', '-o', '%s' % devpath]
    processutils.execute(*daxio_cmd)