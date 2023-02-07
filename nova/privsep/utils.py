from bees import profiler as p
import errno
import mmap
import os
import random
import sys
from oslo_log import log as logging
from oslo_utils import excutils
LOG = logging.getLogger(__name__)

@p.trace('generate_random_string')
def generate_random_string():
    return str(random.randint(0, sys.maxsize))

@p.trace('supports_direct_io')
def supports_direct_io(dirpath):
    if not hasattr(os, 'O_DIRECT'):
        LOG.debug('This python runtime does not support direct I/O')
        return False
    file_name = '%s.%s' % ('.directio.test', generate_random_string())
    testfile = os.path.join(dirpath, file_name)
    hasDirectIO = True
    fd = None
    try:
        fd = os.open(testfile, os.O_CREAT | os.O_WRONLY | os.O_DIRECT)
        align_size = 4096
        m = mmap.mmap(-1, align_size)
        m.write(b'x' * align_size)
        os.write(fd, m)
        LOG.debug("Path '%(path)s' supports direct I/O", {'path': dirpath})
    except OSError as e:
        if e.errno in (errno.EINVAL, errno.ENOENT):
            LOG.debug("Path '%(path)s' does not support direct I/O: '%(ex)s'", {'path': dirpath, 'ex': e})
            hasDirectIO = False
        else:
            with excutils.save_and_reraise_exception():
                LOG.error("Error on '%(path)s' while checking direct I/O: '%(ex)s'", {'path': dirpath, 'ex': e})
    except Exception as e:
        with excutils.save_and_reraise_exception():
            LOG.error("Error on '%(path)s' while checking direct I/O: '%(ex)s'", {'path': dirpath, 'ex': e})
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(testfile)
        except Exception:
            pass
    return hasDirectIO