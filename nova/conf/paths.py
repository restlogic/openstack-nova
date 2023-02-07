from bees import profiler as p
import os
from oslo_config import cfg
ALL_OPTS = [cfg.StrOpt('pybasedir', default=os.path.abspath(os.path.join(os.path.dirname(__file__), '../../')), sample_default='<Path>', help='\nThe directory where the Nova python modules are installed.\n\nThis directory is used to store template files for networking and remote\nconsole access. It is also the default path for other config options which\nneed to persist Nova internal data. It is very unlikely that you need to\nchange this option from its default value.\n\nPossible values:\n\n* The full path to a directory.\n\nRelated options:\n\n* ``state_path``\n'), cfg.StrOpt('state_path', default='$pybasedir', help="\nThe top-level directory for maintaining Nova's state.\n\nThis directory is used to store Nova's internal state. It is used by a\nvariety of other config options which derive from this. In some scenarios\n(for example migrations) it makes sense to use a storage location which is\nshared between multiple compute hosts (for example via NFS). Unless the\noption ``instances_path`` gets overwritten, this directory can grow very\nlarge.\n\nPossible values:\n\n* The full path to a directory. Defaults to value provided in ``pybasedir``.\n")]

@p.trace('basedir_def')
def basedir_def(*args):
    """Return an uninterpolated path relative to $pybasedir."""
    return os.path.join('$pybasedir', *args)

@p.trace('state_path_def')
def state_path_def(*args):
    """Return an uninterpolated path relative to $state_path."""
    return os.path.join('$state_path', *args)

@p.trace('register_opts')
def register_opts(conf):
    conf.register_opts(ALL_OPTS)

@p.trace('list_opts')
def list_opts():
    return {'DEFAULT': ALL_OPTS}