from bees import profiler as p
'\nHandling of VM disk images.\n'
import os
from oslo_concurrency import processutils
from oslo_log import log as logging
from oslo_utils import fileutils
from oslo_utils import imageutils
from nova.compute import utils as compute_utils
import nova.conf
from nova import exception
from nova.i18n import _
from nova.image import glance
import nova.privsep.qemu
LOG = logging.getLogger(__name__)
CONF = nova.conf.CONF
IMAGE_API = glance.API()

@p.trace('qemu_img_info')
def qemu_img_info(path, format=None):
    """Return an object containing the parsed output from qemu-img info."""
    if not os.path.exists(path) and (not path.startswith('rbd:')):
        raise exception.DiskNotFound(location=path)
    info = nova.privsep.qemu.unprivileged_qemu_img_info(path, format=format)
    return imageutils.QemuImgInfo(info, format='json')

@p.trace('privileged_qemu_img_info')
def privileged_qemu_img_info(path, format=None, output_format='json'):
    """Return an object containing the parsed output from qemu-img info."""
    if not os.path.exists(path) and (not path.startswith('rbd:')):
        raise exception.DiskNotFound(location=path)
    info = nova.privsep.qemu.privileged_qemu_img_info(path, format=format)
    return imageutils.QemuImgInfo(info, format='json')

@p.trace('convert_image')
def convert_image(source, dest, in_format, out_format, run_as_root=False, compress=False):
    """Convert image to other format."""
    if in_format is None:
        raise RuntimeError('convert_image without input format is a security risk')
    _convert_image(source, dest, in_format, out_format, run_as_root, compress=compress)

@p.trace('convert_image_unsafe')
def convert_image_unsafe(source, dest, out_format, run_as_root=False):
    """Convert image to other format, doing unsafe automatic input format
    detection. Do not call this function.
    """
    _convert_image(source, dest, None, out_format, run_as_root)

@p.trace('_convert_image')
def _convert_image(source, dest, in_format, out_format, run_as_root, compress=False):
    try:
        with compute_utils.disk_ops_semaphore:
            if not run_as_root:
                nova.privsep.qemu.unprivileged_convert_image(source, dest, in_format, out_format, CONF.instances_path, compress)
            else:
                nova.privsep.qemu.convert_image(source, dest, in_format, out_format, CONF.instances_path, compress)
    except processutils.ProcessExecutionError as exp:
        msg = _('Unable to convert image to %(format)s: %(exp)s') % {'format': out_format, 'exp': exp}
        raise exception.ImageUnacceptable(image_id=source, reason=msg)

@p.trace('fetch')
def fetch(context, image_href, path, trusted_certs=None):
    with fileutils.remove_path_on_error(path):
        with compute_utils.disk_ops_semaphore:
            IMAGE_API.download(context, image_href, dest_path=path, trusted_certs=trusted_certs)

@p.trace('get_info')
def get_info(context, image_href):
    return IMAGE_API.get(context, image_href)

@p.trace('fetch_to_raw')
def fetch_to_raw(context, image_href, path, trusted_certs=None):
    path_tmp = '%s.part' % path
    fetch(context, image_href, path_tmp, trusted_certs)
    with fileutils.remove_path_on_error(path_tmp):
        data = qemu_img_info(path_tmp)
        fmt = data.file_format
        if fmt is None:
            raise exception.ImageUnacceptable(reason=_("'qemu-img info' parsing failed."), image_id=image_href)
        backing_file = data.backing_file
        if backing_file is not None:
            raise exception.ImageUnacceptable(image_id=image_href, reason=_('fmt=%(fmt)s backed by: %(backing_file)s') % {'fmt': fmt, 'backing_file': backing_file})
        if fmt != 'raw' and CONF.force_raw_images:
            staged = '%s.converted' % path
            LOG.debug('%s was %s, converting to raw', image_href, fmt)
            with fileutils.remove_path_on_error(staged):
                try:
                    convert_image(path_tmp, staged, fmt, 'raw')
                except exception.ImageUnacceptable as exp:
                    raise exception.ImageUnacceptable(image_id=image_href, reason=_('Unable to convert image to raw: %(exp)s') % {'exp': exp})
                os.unlink(path_tmp)
                data = qemu_img_info(staged)
                if data.file_format != 'raw':
                    raise exception.ImageUnacceptable(image_id=image_href, reason=_('Converted to raw, but format is now %s') % data.file_format)
                os.rename(staged, path)
        else:
            os.rename(path_tmp, path)