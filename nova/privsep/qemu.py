from bees import profiler as p
'\nHelpers for qemu tasks.\n'
import os
from oslo_concurrency import processutils
from oslo_log import log as logging
from oslo_utils import units
from nova import exception
from nova.i18n import _
import nova.privsep.utils
LOG = logging.getLogger(__name__)
QEMU_IMG_LIMITS = processutils.ProcessLimits(cpu_time=30, address_space=1 * units.Gi)

@p.trace('convert_image')
@nova.privsep.sys_admin_pctxt.entrypoint
def convert_image(source, dest, in_format, out_format, instances_path, compress):
    unprivileged_convert_image(source, dest, in_format, out_format, instances_path, compress)

@p.trace('unprivileged_convert_image')
def unprivileged_convert_image(source, dest, in_format, out_format, instances_path, compress):
    if nova.privsep.utils.supports_direct_io(instances_path):
        cache_mode = 'none'
    else:
        cache_mode = 'writeback'
    cmd = ('qemu-img', 'convert', '-t', cache_mode, '-O', out_format)
    if in_format is not None:
        cmd = cmd + ('-f', in_format)
    if compress:
        cmd += ('-c',)
    cmd = cmd + (source, dest)
    processutils.execute(*cmd)

@p.trace('privileged_qemu_img_info')
@nova.privsep.sys_admin_pctxt.entrypoint
def privileged_qemu_img_info(path, format=None):
    """Return an oject containing the parsed output from qemu-img info

    This is a privileged call to qemu-img info using the sys_admin_pctxt
    entrypoint allowing host block devices etc to be accessed.
    """
    return unprivileged_qemu_img_info(path, format=format)

@p.trace('unprivileged_qemu_img_info')
def unprivileged_qemu_img_info(path, format=None):
    """Return an object containing the parsed output from qemu-img info."""
    try:
        if os.path.isdir(path) and os.path.exists(os.path.join(path, 'DiskDescriptor.xml')):
            path = os.path.join(path, 'root.hds')
        cmd = ('env', 'LC_ALL=C', 'LANG=C', 'qemu-img', 'info', path, '--force-share', '--output=json')
        if format is not None:
            cmd = cmd + ('-f', format)
        (out, err) = processutils.execute(*cmd, prlimit=QEMU_IMG_LIMITS)
    except processutils.ProcessExecutionError as exp:
        if exp.exit_code == -9:
            msg = _('qemu-img aborted by prlimits when inspecting %(path)s : %(exp)s') % {'path': path, 'exp': exp}
        elif exp.exit_code == 1 and 'No such file or directory' in exp.stderr:
            raise exception.DiskNotFound(location=path)
        else:
            msg = _('qemu-img failed to execute on %(path)s : %(exp)s') % {'path': path, 'exp': exp}
        raise exception.InvalidDiskInfo(reason=msg)
    if not out:
        msg = _('Failed to run qemu-img info on %(path)s : %(error)s') % {'path': path, 'error': err}
        raise exception.InvalidDiskInfo(reason=msg)
    return out