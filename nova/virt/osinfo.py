from bees import profiler as p
from oslo_log import log as logging
from oslo_utils import importutils
from nova import exception
from nova.objects import fields
libosinfo = None
LOG = logging.getLogger(__name__)

@p.trace_cls('_OsInfoDatabase')
class _OsInfoDatabase(object):
    _instance = None

    def __init__(self):
        global libosinfo
        try:
            if libosinfo is None:
                libosinfo = importutils.import_module('gi.repository.Libosinfo')
        except ImportError as exp:
            LOG.info('Cannot load Libosinfo: (%s)', exp)
        else:
            self.loader = libosinfo.Loader()
            self.loader.process_default_path()
            self.db = self.loader.get_db()
            self.oslist = self.db.get_os_list()

    @classmethod
    def get_instance(cls):
        """Get libosinfo connection
        """
        if cls._instance is None:
            cls._instance = _OsInfoDatabase()
        return cls._instance

    def get_os(self, os_name):
        """Retrieve OS object based on id, unique URI identifier of the OS
           :param os_name: id - the unique operating systemidentifier
                           e.g. http://fedoraproject.org/fedora/21,
                           http://microsoft.com/win/xp,
                           or a
                           short-id - the short name of the OS
                           e.g. fedora21, winxp
           :returns: The operation system object Libosinfo.Os
           :raise exception.OsInfoNotFound: If os hasn't been found
        """
        if libosinfo is None:
            return
        if not os_name:
            raise exception.OsInfoNotFound(os_name='Empty')
        fltr = libosinfo.Filter.new()
        flt_field = 'id' if os_name.startswith('http') else 'short-id'
        fltr.add_constraint(flt_field, os_name)
        filtered = self.oslist.new_filtered(fltr)
        list_len = filtered.get_length()
        if not list_len:
            raise exception.OsInfoNotFound(os_name=os_name)
        return filtered.get_nth(0)

@p.trace_cls('OsInfo')
class OsInfo(object):
    """OS Information Structure
    """

    def __init__(self, os_name):
        self._os_obj = self._get_os_obj(os_name)

    def _get_os_obj(self, os_name):
        if os_name is not None:
            try:
                return _OsInfoDatabase.get_instance().get_os(os_name)
            except exception.NovaException as e:
                LOG.warning('Cannot find OS information - Reason: (%s)', e)

    @property
    def network_model(self):
        if self._os_obj is not None:
            fltr = libosinfo.Filter()
            fltr.add_constraint('class', 'net')
            devs = self._os_obj.get_all_devices(fltr)
            if devs.get_length():
                net_model = devs.get_nth(0).get_name()
                if net_model in ['virtio-net', 'virtio1.0-net']:
                    return 'virtio'
                if net_model in fields.VIFModel.ALL:
                    return net_model

    @property
    def disk_model(self):
        if self._os_obj is not None:
            fltr = libosinfo.Filter()
            fltr.add_constraint('class', 'block')
            devs = self._os_obj.get_all_devices(fltr)
            if devs.get_length():
                disk_model = devs.get_nth(0).get_name()
                if disk_model in ['virtio-block', 'virtio1.0-block']:
                    return 'virtio'
                if disk_model in fields.DiskBus.ALL:
                    return disk_model

@p.trace_cls('HardwareProperties')
class HardwareProperties(object):

    def __init__(self, image_meta):
        """:param image_meta:  ImageMeta object
        """
        self.img_props = image_meta.properties
        os_key = self.img_props.get('os_distro')
        self.os_info_obj = OsInfo(os_key)

    @property
    def network_model(self):
        return self.img_props.get('hw_vif_model', self.os_info_obj.network_model)

    @property
    def disk_model(self):
        return self.img_props.get('hw_disk_bus', self.os_info_obj.disk_model)