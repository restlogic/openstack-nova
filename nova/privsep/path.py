from bees import profiler as p
'Routines that bypass file-system checks.'
import errno
import os
import shutil
from oslo_utils import fileutils
from nova import exception
import nova.privsep

@p.trace('writefile')
@nova.privsep.sys_admin_pctxt.entrypoint
def writefile(path, mode, content):
    if not os.path.exists(os.path.dirname(path)):
        raise exception.FileNotFound(file_path=path)
    with open(path, mode) as f:
        f.write(content)

@p.trace('chown')
@nova.privsep.sys_admin_pctxt.entrypoint
def chown(path: str, uid: int=-1, gid: int=-1, recursive: bool=False) -> None:
    if not os.path.exists(path):
        raise exception.FileNotFound(file_path=path)
    if not recursive or os.path.isfile(path):
        return os.chown(path, uid, gid)
    for (root, dirs, files) in os.walk(path):
        os.chown(root, uid, gid)
        for item in dirs:
            os.chown(os.path.join(root, item), uid, gid)
        for item in files:
            os.chown(os.path.join(root, item), uid, gid)

@p.trace('makedirs')
@nova.privsep.sys_admin_pctxt.entrypoint
def makedirs(path):
    fileutils.ensure_tree(path)

@p.trace('chmod')
@nova.privsep.sys_admin_pctxt.entrypoint
def chmod(path, mode):
    if not os.path.exists(path):
        raise exception.FileNotFound(file_path=path)
    os.chmod(path, mode)

@p.trace('move_tree')
@nova.privsep.sys_admin_pctxt.entrypoint
def move_tree(source_path: str, dest_path: str) -> None:
    shutil.move(source_path, dest_path)

@p.trace('utime')
@nova.privsep.sys_admin_pctxt.entrypoint
def utime(path):
    if not os.path.exists(path):
        raise exception.FileNotFound(file_path=path)
    os.utime(path, None)

@p.trace('rmdir')
@nova.privsep.sys_admin_pctxt.entrypoint
def rmdir(path):
    if not os.path.exists(path):
        raise exception.FileNotFound(file_path=path)
    os.rmdir(path)

@p.trace('last_bytes')
@nova.privsep.sys_admin_pctxt.entrypoint
def last_bytes(path, num):
    """Return num bytes from the end of the file, and remaining byte count.

    :param path: The file to read
    :param num: The number of bytes to return

    :returns: (data, remaining)
    """
    with open(path, 'rb') as f:
        try:
            f.seek(-num, os.SEEK_END)
        except IOError as e:
            if e.errno == errno.EINVAL:
                f.seek(0, os.SEEK_SET)
            else:
                raise
        remaining = f.tell()
        return (f.read(), remaining)