from bees import profiler as p
import os
import time
from oslo_config import cfg
from oslo_log import log as logging
from oslo_serialization import jsonutils
from nova import utils
LOG = logging.getLogger(__name__)
CONF = cfg.CONF
TWENTY_FOUR_HOURS = 3600 * 24

@p.trace('register_storage_use')
def register_storage_use(storage_path, hostname):
    """Identify the id of this instance storage."""
    LOCK_PATH = os.path.join(CONF.instances_path, 'locks')

    @utils.synchronized('storage-registry-lock', external=True, lock_path=LOCK_PATH)
    def do_register_storage_use(storage_path, hostname):
        d = {}
        id_path = os.path.join(storage_path, 'compute_nodes')
        if os.path.exists(id_path):
            with open(id_path) as f:
                try:
                    d = jsonutils.loads(f.read())
                except ValueError:
                    LOG.warning('Cannot decode JSON from %(id_path)s', {'id_path': id_path})
        d[hostname] = time.time()
        with open(id_path, 'w') as f:
            f.write(jsonutils.dumps(d))
    return do_register_storage_use(storage_path, hostname)

@p.trace('get_storage_users')
def get_storage_users(storage_path):
    """Get a list of all the users of this storage path."""
    LOCK_PATH = os.path.join(CONF.instances_path, 'locks')

    @utils.synchronized('storage-registry-lock', external=True, lock_path=LOCK_PATH)
    def do_get_storage_users(storage_path):
        d = {}
        id_path = os.path.join(storage_path, 'compute_nodes')
        if os.path.exists(id_path):
            with open(id_path) as f:
                try:
                    d = jsonutils.loads(f.read())
                except ValueError:
                    LOG.warning('Cannot decode JSON from %(id_path)s', {'id_path': id_path})
        recent_users = []
        for node in d:
            if time.time() - d[node] < TWENTY_FOUR_HOURS:
                recent_users.append(node)
        return recent_users
    return do_get_storage_users(storage_path)