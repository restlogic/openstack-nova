from bees import profiler as p
'Handles all requests relating to compute resources (e.g. guest VMs,\nnetworking and storage of VMs, and compute hosts on which they run).'
import collections
import functools
import re
import string
import typing as ty
from castellan import key_manager
import os_traits
from oslo_log import log as logging
from oslo_messaging import exceptions as oslo_exceptions
from oslo_serialization import base64 as base64utils
from oslo_utils import excutils
from oslo_utils import strutils
from oslo_utils import timeutils
from oslo_utils import units
from oslo_utils import uuidutils
from nova.accelerator import cyborg
from nova import availability_zones
from nova import block_device
from nova.compute import flavors
from nova.compute import instance_actions
from nova.compute import instance_list
from nova.compute import migration_list
from nova.compute import power_state
from nova.compute import rpcapi as compute_rpcapi
from nova.compute import task_states
from nova.compute import utils as compute_utils
from nova.compute.utils import wrap_instance_event
from nova.compute import vm_states
from nova import conductor
import nova.conf
from nova import context as nova_context
from nova import crypto
from nova.db import base
from nova.db.sqlalchemy import api as db_api
from nova import exception
from nova import exception_wrapper
from nova.i18n import _
from nova.image import glance
from nova.network import constants
from nova.network import model as network_model
from nova.network import neutron
from nova.network import security_group_api
from nova import objects
from nova.objects import block_device as block_device_obj
from nova.objects import external_event as external_event_obj
from nova.objects import fields as fields_obj
from nova.objects import image_meta as image_meta_obj
from nova.objects import keypair as keypair_obj
from nova.objects import quotas as quotas_obj
from nova.pci import request as pci_request
from nova.policies import servers as servers_policies
import nova.policy
from nova import profiler
from nova import rpc
from nova.scheduler.client import query
from nova.scheduler.client import report
from nova.scheduler import utils as scheduler_utils
from nova import servicegroup
from nova import utils
from nova.virt import hardware
from nova.volume import cinder
LOG = logging.getLogger(__name__)
wrap_exception = functools.partial(exception_wrapper.wrap_exception, service='compute', binary='nova-api')
CONF = nova.conf.CONF
AGGREGATE_ACTION_UPDATE = 'Update'
AGGREGATE_ACTION_UPDATE_META = 'UpdateMeta'
AGGREGATE_ACTION_DELETE = 'Delete'
AGGREGATE_ACTION_ADD = 'Add'
MIN_COMPUTE_SYNC_COMPUTE_STATUS_DISABLED = 38
MIN_COMPUTE_CROSS_CELL_RESIZE = 47
MIN_COMPUTE_SAME_HOST_COLD_MIGRATE = 48
MIN_VER_NOVA_COMPUTE_MIXED_POLICY = 52
SUPPORT_ACCELERATOR_SERVICE_FOR_REBUILD = 53
CELLS = []

@p.trace('check_instance_state')
def check_instance_state(vm_state=None, task_state=(None,), must_have_launched=True):
    """Decorator to check VM and/or task state before entry to API functions.

    If the instance is in the wrong state, or has not been successfully
    started at least once the wrapper will raise an exception.
    """
    if vm_state is not None and (not isinstance(vm_state, set)):
        vm_state = set(vm_state)
    if task_state is not None and (not isinstance(task_state, set)):
        task_state = set(task_state)

    def outer(f):

        @functools.wraps(f)
        def inner(self, context, instance, *args, **kw):
            if vm_state is not None and instance.vm_state not in vm_state:
                raise exception.InstanceInvalidState(attr='vm_state', instance_uuid=instance.uuid, state=instance.vm_state, method=f.__name__)
            if task_state is not None and instance.task_state not in task_state:
                raise exception.InstanceInvalidState(attr='task_state', instance_uuid=instance.uuid, state=instance.task_state, method=f.__name__)
            if must_have_launched and (not instance.launched_at):
                raise exception.InstanceInvalidState(attr='launched_at', instance_uuid=instance.uuid, state=instance.launched_at, method=f.__name__)
            return f(self, context, instance, *args, **kw)
        return inner
    return outer

@p.trace('_set_or_none')
def _set_or_none(q):
    return q if q is None or isinstance(q, set) else set(q)

@p.trace('reject_instance_state')
def reject_instance_state(vm_state=None, task_state=None):
    """Decorator.  Raise InstanceInvalidState if instance is in any of the
    given states.
    """
    vm_state = _set_or_none(vm_state)
    task_state = _set_or_none(task_state)

    def outer(f):

        @functools.wraps(f)
        def inner(self, context, instance, *args, **kw):
            _InstanceInvalidState = functools.partial(exception.InstanceInvalidState, instance_uuid=instance.uuid, method=f.__name__)
            if vm_state is not None and instance.vm_state in vm_state:
                raise _InstanceInvalidState(attr='vm_state', state=instance.vm_state)
            if task_state is not None and instance.task_state in task_state:
                raise _InstanceInvalidState(attr='task_state', state=instance.task_state)
            return f(self, context, instance, *args, **kw)
        return inner
    return outer

@p.trace('check_instance_host')
def check_instance_host(check_is_up=False):
    """Validate the instance.host before performing the operation.

    At a minimum this method will check that the instance.host is set.

    :param check_is_up: If True, check that the instance.host status is UP
        or MAINTENANCE (disabled but not down).
    :raises: InstanceNotReady if the instance.host is not set
    :raises: ServiceUnavailable if check_is_up=True and the instance.host
        compute service status is not UP or MAINTENANCE
    """

    def outer(function):

        @functools.wraps(function)
        def wrapped(self, context, instance, *args, **kwargs):
            if not instance.host:
                raise exception.InstanceNotReady(instance_id=instance.uuid)
            if check_is_up:
                host_status = self.get_instance_host_status(instance)
                if host_status not in (fields_obj.HostStatus.UP, fields_obj.HostStatus.MAINTENANCE):
                    raise exception.ServiceUnavailable()
            return function(self, context, instance, *args, **kwargs)
        return wrapped
    return outer

@p.trace('check_instance_lock')
def check_instance_lock(function):

    @functools.wraps(function)
    def inner(self, context, instance, *args, **kwargs):
        if instance.locked and (not context.is_admin):
            raise exception.InstanceIsLocked(instance_uuid=instance.uuid)
        return function(self, context, instance, *args, **kwargs)
    return inner

@p.trace('reject_sev_instances')
def reject_sev_instances(operation):
    """Reject requests to decorated function if instance has SEV enabled.

    Raise OperationNotSupportedForSEV if instance has SEV enabled.
    """

    def outer(f):

        @functools.wraps(f)
        def inner(self, context, instance, *args, **kw):
            if hardware.get_mem_encryption_constraint(instance.flavor, instance.image_meta):
                raise exception.OperationNotSupportedForSEV(instance_uuid=instance.uuid, operation=operation)
            return f(self, context, instance, *args, **kw)
        return inner
    return outer

@p.trace('reject_vtpm_instances')
def reject_vtpm_instances(operation):
    """Reject requests to decorated function if instance has vTPM enabled.

    Raise OperationNotSupportedForVTPM if instance has vTPM enabled.
    """

    def outer(f):

        @functools.wraps(f)
        def inner(self, context, instance, *args, **kw):
            if hardware.get_vtpm_constraint(instance.flavor, instance.image_meta):
                raise exception.OperationNotSupportedForVTPM(instance_uuid=instance.uuid, operation=operation)
            return f(self, context, instance, *args, **kw)
        return inner
    return outer

@p.trace('reject_vdpa_instances')
def reject_vdpa_instances(operation):
    """Reject requests to decorated function if instance has vDPA interfaces.

    Raise OperationNotSupportedForVDPAInterfaces if operations involves one or
        more vDPA interfaces.
    """

    def outer(f):

        @functools.wraps(f)
        def inner(self, context, instance, *args, **kw):
            if any((vif['vnic_type'] == network_model.VNIC_TYPE_VDPA for vif in instance.get_network_info())):
                raise exception.OperationNotSupportedForVDPAInterface(instance_uuid=instance.uuid, operation=operation)
            return f(self, context, instance, *args, **kw)
        return inner
    return outer

@p.trace('load_cells')
def load_cells():
    global CELLS
    if not CELLS:
        CELLS = objects.CellMappingList.get_all(nova_context.get_admin_context())
        LOG.debug('Found %(count)i cells: %(cells)s', dict(count=len(CELLS), cells=','.join([c.identity for c in CELLS])))
    if not CELLS:
        LOG.error('No cells are configured, unable to continue')

@p.trace('_get_image_meta_obj')
def _get_image_meta_obj(image_meta_dict):
    try:
        image_meta = objects.ImageMeta.from_dict(image_meta_dict)
    except ValueError as e:
        msg = _('Invalid image metadata. Error: %s') % str(e)
        raise exception.InvalidRequest(msg)
    return image_meta

@p.trace('block_accelerators')
def block_accelerators(until_service=None):

    def inner(func):

        @functools.wraps(func)
        def wrapper(self, context, instance, *args, **kwargs):
            dp_name = instance.flavor.extra_specs.get('accel:device_profile')
            service_support = False
            if not dp_name:
                service_support = True
            elif until_service:
                min_version = objects.service.get_minimum_version_all_cells(nova_context.get_admin_context(), ['nova-compute'])
                if min_version >= until_service:
                    service_support = True
            if not service_support:
                raise exception.ForbiddenWithAccelerators()
            return func(self, context, instance, *args, **kwargs)
        return wrapper
    return inner

@p.trace_cls('API')
@profiler.trace_cls('compute_api')
class API(base.Base):
    """API for interacting with the compute manager."""

    def __init__(self, image_api=None, network_api=None, volume_api=None, **kwargs):
        self.image_api = image_api or glance.API()
        self.network_api = network_api or neutron.API()
        self.volume_api = volume_api or cinder.API()
        self._placementclient = None
        self.compute_rpcapi = compute_rpcapi.ComputeAPI()
        self.compute_task_api = conductor.ComputeTaskAPI()
        self.servicegroup_api = servicegroup.API()
        self.host_api = HostAPI(self.compute_rpcapi, self.servicegroup_api)
        self.notifier = rpc.get_notifier('compute')
        if CONF.ephemeral_storage_encryption.enabled:
            self.key_manager = key_manager.API()
        self.host = CONF.host
        super(API, self).__init__(**kwargs)

    def _record_action_start(self, context, instance, action):
        objects.InstanceAction.action_start(context, instance.uuid, action, want_result=False)

    def _check_injected_file_quota(self, context, injected_files):
        """Enforce quota limits on injected files.

        Raises a QuotaError if any limit is exceeded.
        """
        if not injected_files:
            return
        try:
            objects.Quotas.limit_check(context, injected_files=len(injected_files))
        except exception.OverQuota:
            raise exception.OnsetFileLimitExceeded()
        max_path = 0
        max_content = 0
        for (path, content) in injected_files:
            max_path = max(max_path, len(path))
            max_content = max(max_content, len(content))
        try:
            objects.Quotas.limit_check(context, injected_file_path_bytes=max_path, injected_file_content_bytes=max_content)
        except exception.OverQuota as exc:
            if 'injected_file_path_bytes' in exc.kwargs['overs']:
                raise exception.OnsetFilePathLimitExceeded(allowed=exc.kwargs['quotas']['injected_file_path_bytes'])
            else:
                raise exception.OnsetFileContentLimitExceeded(allowed=exc.kwargs['quotas']['injected_file_content_bytes'])

    def _check_metadata_properties_quota(self, context, metadata=None):
        """Enforce quota limits on metadata properties."""
        if not metadata:
            return
        if not isinstance(metadata, dict):
            msg = _('Metadata type should be dict.')
            raise exception.InvalidMetadata(reason=msg)
        num_metadata = len(metadata)
        try:
            objects.Quotas.limit_check(context, metadata_items=num_metadata)
        except exception.OverQuota as exc:
            quota_metadata = exc.kwargs['quotas']['metadata_items']
            raise exception.MetadataLimitExceeded(allowed=quota_metadata)
        for (k, v) in metadata.items():
            try:
                utils.check_string_length(v)
                utils.check_string_length(k, min_length=1)
            except exception.InvalidInput as e:
                raise exception.InvalidMetadata(reason=e.format_message())
            if len(k) > 255:
                msg = _('Metadata property key greater than 255 characters')
                raise exception.InvalidMetadataSize(reason=msg)
            if len(v) > 255:
                msg = _('Metadata property value greater than 255 characters')
                raise exception.InvalidMetadataSize(reason=msg)

    def _check_requested_secgroups(self, context, secgroups):
        """Check if the security group requested exists and belongs to
        the project.

        :param context: The nova request context.
        :type context: nova.context.RequestContext
        :param secgroups: list of requested security group names
        :type secgroups: list
        :returns: list of requested security group UUIDs; note that 'default'
            is a special case and will be unmodified if it's requested.
        """
        security_groups = []
        for secgroup in secgroups:
            if secgroup == 'default':
                security_groups.append(secgroup)
                continue
            secgroup_uuid = security_group_api.validate_name(context, secgroup)
            security_groups.append(secgroup_uuid)
        return security_groups

    def _check_requested_networks(self, context, requested_networks, max_count):
        """Check if the networks requested belongs to the project
        and the fixed IP address for each network provided is within
        same the network block
        """
        if requested_networks is not None:
            if requested_networks.no_allocate:
                return max_count
            requested_networks = requested_networks.as_tuples()
        return self.network_api.validate_networks(context, requested_networks, max_count)

    def _handle_kernel_and_ramdisk(self, context, kernel_id, ramdisk_id, image):
        """Choose kernel and ramdisk appropriate for the instance.

        The kernel and ramdisk can be chosen in one of two ways:

            1. Passed in with create-instance request.

            2. Inherited from image metadata.

        If inherited from image metadata, and if that image metadata value is
        set to 'nokernel', both kernel and ramdisk will default to None.
        """
        image_properties = image.get('properties', {})
        if kernel_id is None:
            kernel_id = image_properties.get('kernel_id')
        if ramdisk_id is None:
            ramdisk_id = image_properties.get('ramdisk_id')
        if kernel_id == 'nokernel':
            kernel_id = None
            ramdisk_id = None
        if kernel_id is not None:
            kernel_image = self.image_api.get(context, kernel_id)
            kernel_id = kernel_image['id']
        if ramdisk_id is not None:
            ramdisk_image = self.image_api.get(context, ramdisk_id)
            ramdisk_id = ramdisk_image['id']
        return (kernel_id, ramdisk_id)

    @staticmethod
    def parse_availability_zone(context, availability_zone):
        forced_host = None
        forced_node = None
        if availability_zone and ':' in availability_zone:
            c = availability_zone.count(':')
            if c == 1:
                (availability_zone, forced_host) = availability_zone.split(':')
            elif c == 2:
                if '::' in availability_zone:
                    (availability_zone, forced_node) = availability_zone.split('::')
                else:
                    (availability_zone, forced_host, forced_node) = availability_zone.split(':')
            else:
                raise exception.InvalidInput(reason='Unable to parse availability_zone')
        if not availability_zone:
            availability_zone = CONF.default_schedule_zone
        return (availability_zone, forced_host, forced_node)

    def _ensure_auto_disk_config_is_valid(self, auto_disk_config_img, auto_disk_config, image):
        auto_disk_config_disabled = utils.is_auto_disk_config_disabled(auto_disk_config_img)
        if auto_disk_config_disabled and auto_disk_config:
            raise exception.AutoDiskConfigDisabledByImage(image=image)

    def _inherit_properties_from_image(self, image, auto_disk_config):
        image_properties = image.get('properties', {})
        auto_disk_config_img = utils.get_auto_disk_config_from_image_props(image_properties)
        self._ensure_auto_disk_config_is_valid(auto_disk_config_img, auto_disk_config, image.get('id'))
        if auto_disk_config is None:
            auto_disk_config = strutils.bool_from_string(auto_disk_config_img)
        return {'os_type': image_properties.get('os_type'), 'architecture': image_properties.get('architecture'), 'vm_mode': image_properties.get('vm_mode'), 'auto_disk_config': auto_disk_config}

    def _check_config_drive(self, config_drive):
        if config_drive:
            try:
                bool_val = strutils.bool_from_string(config_drive, strict=True)
            except ValueError:
                raise exception.ConfigDriveInvalidValue(option=config_drive)
        else:
            bool_val = False
        return True if bool_val else ''

    def _validate_flavor_image(self, context, image_id, image, instance_type, root_bdm, validate_numa=True):
        """Validate the flavor and image.

        This is called from the API service to ensure that the flavor
        extra-specs and image properties are self-consistent and compatible
        with each other.

        :param context: A context.RequestContext
        :param image_id: UUID of the image
        :param image: a dict representation of the image including properties,
                      enforces the image status is active.
        :param instance_type: Flavor object
        :param root_bdm: BlockDeviceMapping for root disk.  Will be None for
               the resize case.
        :param validate_numa: Flag to indicate whether or not to validate
               the NUMA-related metadata.
        :raises: Many different possible exceptions.  See
                 api.openstack.compute.servers.INVALID_FLAVOR_IMAGE_EXCEPTIONS
                 for the full list.
        """
        if image and image['status'] != 'active':
            raise exception.ImageNotActive(image_id=image_id)
        self._validate_flavor_image_nostatus(context, image, instance_type, root_bdm, validate_numa)

    @staticmethod
    def _detect_nonbootable_image_from_properties(image_id, image):
        """Check image for a property indicating it's nonbootable.

        This is called from the API service to ensure that there are
        no known image properties indicating that this image is of a
        type that we do not support booting from.

        Currently the only such property is 'cinder_encryption_key_id'.

        :param image_id: UUID of the image
        :param image: a dict representation of the image including properties
        :raises: ImageUnacceptable if the image properties indicate
                 that booting this image is not supported
        """
        if not image:
            return
        image_properties = image.get('properties', {})
        if image_properties.get('cinder_encryption_key_id') and image_id:
            reason = _('Direct booting of an image uploaded from an encrypted volume is unsupported.')
            raise exception.ImageUnacceptable(image_id=image_id, reason=reason)

    @staticmethod
    def _validate_flavor_image_nostatus(context, image, instance_type, root_bdm, validate_numa=True, validate_pci=False):
        """Validate the flavor and image.

        This is called from the API service to ensure that the flavor
        extra-specs and image properties are self-consistent and compatible
        with each other.

        :param context: A context.RequestContext
        :param image: a dict representation of the image including properties
        :param instance_type: Flavor object
        :param root_bdm: BlockDeviceMapping for root disk.  Will be None for
               the resize case.
        :param validate_numa: Flag to indicate whether or not to validate
               the NUMA-related metadata.
        :param validate_pci: Flag to indicate whether or not to validate
               the PCI-related metadata.
        :raises: Many different possible exceptions.  See
                 api.openstack.compute.servers.INVALID_FLAVOR_IMAGE_EXCEPTIONS
                 for the full list.
        """
        if not image:
            return
        image_properties = image.get('properties', {})
        config_drive_option = image_properties.get('img_config_drive', 'optional')
        if config_drive_option not in ['optional', 'mandatory']:
            raise exception.InvalidImageConfigDrive(config_drive=config_drive_option)
        if instance_type['memory_mb'] < int(image.get('min_ram') or 0):
            raise exception.FlavorMemoryTooSmall()
        image_min_disk = int(image.get('min_disk') or 0) * units.Gi
        image_size = int(image.get('size') or 0)
        if root_bdm is not None and root_bdm.is_volume:
            dest_size = root_bdm.volume_size
            if dest_size is not None:
                dest_size *= units.Gi
                if image_min_disk > dest_size:
                    raise exception.VolumeSmallerThanMinDisk(volume_size=dest_size, image_min_disk=image_min_disk)
        else:
            dest_size = instance_type['root_gb'] * units.Gi
            if dest_size != 0:
                if image_size > dest_size:
                    raise exception.FlavorDiskSmallerThanImage(flavor_size=dest_size, image_size=image_size)
                if image_min_disk > dest_size:
                    raise exception.FlavorDiskSmallerThanMinDisk(flavor_size=dest_size, image_min_disk=image_min_disk)
            elif not context.can(servers_policies.ZERO_DISK_FLAVOR, fatal=False):
                raise exception.BootFromVolumeRequiredForZeroDiskFlavor()
        API._validate_flavor_image_numa_pci(image, instance_type, validate_numa=validate_numa, validate_pci=validate_pci)

    @staticmethod
    def _check_compute_service_for_mixed_instance(numa_topology):
        """Check if the nova-compute service is ready to support mixed instance
        when the CPU allocation policy is 'mixed'.
        """
        if numa_topology is None:
            return
        if numa_topology.cpu_policy != fields_obj.CPUAllocationPolicy.MIXED:
            return
        minimal_version = objects.service.get_minimum_version_all_cells(nova_context.get_admin_context(), ['nova-compute'])
        if minimal_version < MIN_VER_NOVA_COMPUTE_MIXED_POLICY:
            raise exception.MixedInstanceNotSupportByComputeService()

    @staticmethod
    def _validate_flavor_image_numa_pci(image, instance_type, validate_numa=True, validate_pci=False):
        """Validate the flavor and image NUMA/PCI values.

        This is called from the API service to ensure that the flavor
        extra-specs and image properties are self-consistent and compatible
        with each other.

        :param image: a dict representation of the image including properties
        :param instance_type: Flavor object
        :param validate_numa: Flag to indicate whether or not to validate
               the NUMA-related metadata.
        :param validate_pci: Flag to indicate whether or not to validate
               the PCI-related metadata.
        :raises: Many different possible exceptions.  See
                 api.openstack.compute.servers.INVALID_FLAVOR_IMAGE_EXCEPTIONS
                 for the full list.
        """
        image_meta = _get_image_meta_obj(image)
        API._validate_flavor_image_mem_encryption(instance_type, image_meta)
        flavor_pmu = instance_type.extra_specs.get('hw:pmu')
        image_pmu = image_meta.properties.get('hw_pmu')
        if flavor_pmu is not None and image_pmu is not None and (image_pmu != strutils.bool_from_string(flavor_pmu)):
            raise exception.ImagePMUConflict()
        hardware.get_number_of_serial_ports(instance_type, image_meta)
        hardware.get_realtime_cpu_constraint(instance_type, image_meta)
        hardware.get_cpu_topology_constraints(instance_type, image_meta)
        if validate_numa:
            hardware.numa_get_constraints(instance_type, image_meta)
        if validate_pci:
            pci_request.get_pci_requests_from_flavor(instance_type)

    @staticmethod
    def _validate_flavor_image_mem_encryption(instance_type, image):
        """Validate that the flavor and image don't make contradictory
        requests regarding memory encryption.

        :param instance_type: Flavor object
        :param image: an ImageMeta object
        :raises: nova.exception.FlavorImageConflict
        """
        hardware.get_mem_encryption_constraint(instance_type, image)

    def _get_image_defined_bdms(self, instance_type, image_meta, root_device_name):
        image_properties = image_meta.get('properties', {})
        image_defined_bdms = image_properties.get('block_device_mapping', [])
        legacy_image_defined = not image_properties.get('bdm_v2', False)
        image_mapping = image_properties.get('mappings', [])
        if legacy_image_defined:
            image_defined_bdms = block_device.from_legacy_mapping(image_defined_bdms, None, root_device_name)
        else:
            image_defined_bdms = list(map(block_device.BlockDeviceDict, image_defined_bdms))
        if image_mapping:
            image_mapping = self._prepare_image_mapping(instance_type, image_mapping)
            image_defined_bdms = self._merge_bdms_lists(image_mapping, image_defined_bdms)
        return image_defined_bdms

    def _get_flavor_defined_bdms(self, instance_type, block_device_mapping):
        flavor_defined_bdms = []
        have_ephemeral_bdms = any(filter(block_device.new_format_is_ephemeral, block_device_mapping))
        have_swap_bdms = any(filter(block_device.new_format_is_swap, block_device_mapping))
        if instance_type.get('ephemeral_gb') and (not have_ephemeral_bdms):
            flavor_defined_bdms.append(block_device.create_blank_bdm(instance_type['ephemeral_gb']))
        if instance_type.get('swap') and (not have_swap_bdms):
            flavor_defined_bdms.append(block_device.create_blank_bdm(instance_type['swap'], 'swap'))
        return flavor_defined_bdms

    def _merge_bdms_lists(self, overridable_mappings, overrider_mappings):
        """Override any block devices from the first list by device name

        :param overridable_mappings: list which items are overridden
        :param overrider_mappings: list which items override

        :returns: A merged list of bdms
        """
        device_names = set((bdm['device_name'] for bdm in overrider_mappings if bdm['device_name']))
        return overrider_mappings + [bdm for bdm in overridable_mappings if bdm['device_name'] not in device_names]

    def _check_and_transform_bdm(self, context, base_options, instance_type, image_meta, min_count, max_count, block_device_mapping, legacy_bdm):
        root_device_name = base_options.get('root_device_name') or 'vda'
        image_ref = base_options.get('image_ref', '')
        if image_ref:
            for bdm in block_device_mapping:
                if bdm.get('destination_type') == 'volume' and block_device.strip_dev(bdm.get('device_name')) == root_device_name:
                    msg = _('The volume cannot be assigned the same device name as the root device %s') % root_device_name
                    raise exception.InvalidRequest(msg)
        image_defined_bdms = self._get_image_defined_bdms(instance_type, image_meta, root_device_name)
        root_in_image_bdms = block_device.get_root_bdm(image_defined_bdms) is not None
        if legacy_bdm:
            block_device_mapping = block_device.from_legacy_mapping(block_device_mapping, image_ref, root_device_name, no_root=root_in_image_bdms)
        elif root_in_image_bdms:

            def not_image_and_root_bdm(bdm):
                return not (bdm.get('boot_index') == 0 and bdm.get('source_type') == 'image')
            block_device_mapping = list(filter(not_image_and_root_bdm, block_device_mapping))
        block_device_mapping = self._merge_bdms_lists(image_defined_bdms, block_device_mapping)
        if min_count > 1 or max_count > 1:
            if any(map(lambda bdm: bdm['source_type'] == 'volume', block_device_mapping)):
                msg = _('Cannot attach one or more volumes to multiple instances')
                raise exception.InvalidRequest(msg)
        block_device_mapping += self._get_flavor_defined_bdms(instance_type, block_device_mapping)
        return block_device_obj.block_device_make_list_from_dicts(context, block_device_mapping)

    def _get_image(self, context, image_href):
        if not image_href:
            return (None, {})
        image = self.image_api.get(context, image_href)
        return (image['id'], image)

    def _checks_for_create_and_rebuild(self, context, image_id, image, instance_type, metadata, files_to_inject, root_bdm, validate_numa=True):
        self._check_metadata_properties_quota(context, metadata)
        self._check_injected_file_quota(context, files_to_inject)
        self._detect_nonbootable_image_from_properties(image_id, image)
        self._validate_flavor_image(context, image_id, image, instance_type, root_bdm, validate_numa=validate_numa)

    def _validate_and_build_base_options(self, context, instance_type, boot_meta, image_href, image_id, kernel_id, ramdisk_id, display_name, display_description, key_name, key_data, security_groups, availability_zone, user_data, metadata, access_ip_v4, access_ip_v6, requested_networks, config_drive, auto_disk_config, reservation_id, max_count, supports_port_resource_request):
        """Verify all the input parameters regardless of the provisioning
        strategy being performed.
        """
        if instance_type['disabled']:
            raise exception.FlavorNotFound(flavor_id=instance_type['id'])
        if user_data:
            try:
                base64utils.decode_as_bytes(user_data)
            except TypeError:
                raise exception.InstanceUserDataMalformed()
        security_groups = self._check_requested_secgroups(context, security_groups)
        max_network_count = self._check_requested_networks(context, requested_networks, max_count)
        (kernel_id, ramdisk_id) = self._handle_kernel_and_ramdisk(context, kernel_id, ramdisk_id, boot_meta)
        config_drive = self._check_config_drive(config_drive)
        if key_data is None and key_name is not None:
            key_pair = objects.KeyPair.get_by_name(context, context.user_id, key_name)
            key_data = key_pair.public_key
        else:
            key_pair = None
        root_device_name = block_device.prepend_dev(block_device.properties_root_device_name(boot_meta.get('properties', {})))
        image_meta = _get_image_meta_obj(boot_meta)
        numa_topology = hardware.numa_get_constraints(instance_type, image_meta)
        system_metadata = {}
        pci_numa_affinity_policy = hardware.get_pci_numa_policy_constraint(instance_type, image_meta)
        pci_request_info = pci_request.get_pci_requests_from_flavor(instance_type, affinity_policy=pci_numa_affinity_policy)
        result = self.network_api.create_resource_requests(context, requested_networks, pci_request_info, affinity_policy=pci_numa_affinity_policy)
        (network_metadata, port_resource_requests) = result
        if port_resource_requests and (not supports_port_resource_request):
            raise exception.CreateWithPortResourceRequestOldVersion()
        base_options = {'reservation_id': reservation_id, 'image_ref': image_href, 'kernel_id': kernel_id or '', 'ramdisk_id': ramdisk_id or '', 'power_state': power_state.NOSTATE, 'vm_state': vm_states.BUILDING, 'config_drive': config_drive, 'user_id': context.user_id, 'project_id': context.project_id, 'instance_type_id': instance_type['id'], 'memory_mb': instance_type['memory_mb'], 'vcpus': instance_type['vcpus'], 'root_gb': instance_type['root_gb'], 'ephemeral_gb': instance_type['ephemeral_gb'], 'display_name': display_name, 'display_description': display_description, 'user_data': user_data, 'key_name': key_name, 'key_data': key_data, 'locked': False, 'metadata': metadata or {}, 'access_ip_v4': access_ip_v4, 'access_ip_v6': access_ip_v6, 'availability_zone': availability_zone, 'root_device_name': root_device_name, 'progress': 0, 'pci_requests': pci_request_info, 'numa_topology': numa_topology, 'system_metadata': system_metadata, 'port_resource_requests': port_resource_requests}
        options_from_image = self._inherit_properties_from_image(boot_meta, auto_disk_config)
        base_options.update(options_from_image)
        return (base_options, max_network_count, key_pair, security_groups, network_metadata)

    @staticmethod
    @db_api.api_context_manager.writer
    def _create_reqspec_buildreq_instmapping(context, rs, br, im):
        """Create the request spec, build request, and instance mapping in a
        single database transaction.

        The RequestContext must be passed in to this method so that the
        database transaction context manager decorator will nest properly and
        include each create() into the same transaction context.
        """
        rs.create()
        br.create()
        im.create()

    def _validate_host_or_node(self, context, host, hypervisor_hostname):
        """Check whether compute nodes exist by validating the host
        and/or the hypervisor_hostname. There are three cases:
        1. If only host is supplied, we can lookup the HostMapping in
        the API DB.
        2. If only node is supplied, we can query a resource provider
        with that name in placement.
        3. If both host and node are supplied, we can get the cell from
        HostMapping and from that lookup the ComputeNode with the
        given cell.

        :param context: The API request context.
        :param host: Target host.
        :param hypervisor_hostname: Target node.
        :raises: ComputeHostNotFound if we find no compute nodes with host
                 and/or hypervisor_hostname.
        """
        if host:
            try:
                host_mapping = objects.HostMapping.get_by_host(context, host)
            except exception.HostMappingNotFound:
                LOG.warning('No host-to-cell mapping found for host %(host)s.', {'host': host})
                raise exception.ComputeHostNotFound(host=host)
            if hypervisor_hostname:
                cell = host_mapping.cell_mapping
                with nova_context.target_cell(context, cell) as cctxt:
                    objects.ComputeNode.get_by_host_and_nodename(cctxt, host, hypervisor_hostname)
        elif hypervisor_hostname:
            try:
                self.placementclient.get_provider_by_name(context, hypervisor_hostname)
            except exception.ResourceProviderNotFound:
                raise exception.ComputeHostNotFound(host=hypervisor_hostname)

    def _get_volumes_for_bdms(self, context, bdms):
        """Get the pre-existing volumes from cinder for the list of BDMs.

        :param context: nova auth RequestContext
        :param bdms: BlockDeviceMappingList which has zero or more BDMs with
            a pre-existing volume_id specified.
        :return: dict, keyed by volume id, of volume dicts
        :raises: VolumeNotFound - if a given volume does not exist
        :raises: CinderConnectionFailed - if there are problems communicating
            with the cinder API
        :raises: Forbidden - if the user token does not have authority to see
            a volume
        """
        volumes = {}
        for bdm in bdms:
            if bdm.volume_id:
                volumes[bdm.volume_id] = self.volume_api.get(context, bdm.volume_id)
        return volumes

    @staticmethod
    def _validate_vol_az_for_create(instance_az, volumes):
        """Performs cross_az_attach validation for the instance and volumes.

        If [cinder]/cross_az_attach=True (default) this method is a no-op.

        If [cinder]/cross_az_attach=False, this method will validate that:

        1. All volumes are in the same availability zone.
        2. The volume AZ matches the instance AZ. If the instance is being
           created without a specific AZ (either via the user request or the
           [DEFAULT]/default_schedule_zone option), and the volume AZ matches
           [DEFAULT]/default_availability_zone for compute services, then the
           method returns the volume AZ so it can be set in the RequestSpec as
           if the user requested the zone explicitly.

        :param instance_az: Availability zone for the instance. In this case
            the host is not yet selected so the instance AZ value should come
            from one of the following cases:

            * The user requested availability zone.
            * [DEFAULT]/default_schedule_zone (defaults to None) if the request
              does not specify an AZ (see parse_availability_zone).
        :param volumes: iterable of dicts of cinder volumes to be attached to
            the server being created
        :returns: None or volume AZ to set in the RequestSpec for the instance
        :raises: MismatchVolumeAZException if the instance and volume AZ do
            not match
        """
        if CONF.cinder.cross_az_attach:
            return
        if not volumes:
            return
        vol_zones = [vol['availability_zone'] for vol in volumes]
        if len(set(vol_zones)) > 1:
            msg = _('Volumes are in different availability zones: %s') % ','.join(vol_zones)
            raise exception.MismatchVolumeAZException(reason=msg)
        volume_az = vol_zones[0]
        if instance_az is None:
            if volume_az != CONF.default_availability_zone:
                return volume_az
            return
        if instance_az != volume_az:
            msg = _('Server and volumes are not in the same availability zone. Server is in: %(instance_az)s. Volumes are in: %(volume_az)s') % {'instance_az': instance_az, 'volume_az': volume_az}
            raise exception.MismatchVolumeAZException(reason=msg)

    def _provision_instances(self, context, instance_type, min_count, max_count, base_options, boot_meta, security_groups, block_device_mapping, shutdown_terminate, instance_group, check_server_group_quota, filter_properties, key_pair, tags, trusted_certs, supports_multiattach, network_metadata=None, requested_host=None, requested_hypervisor_hostname=None):
        destination = None
        if requested_host or requested_hypervisor_hostname:
            self._validate_host_or_node(context, requested_host, requested_hypervisor_hostname)
            destination = objects.Destination()
            if requested_host:
                destination.host = requested_host
            destination.node = requested_hypervisor_hostname
        num_instances = compute_utils.check_num_instances_quota(context, instance_type, min_count, max_count)
        security_groups = security_group_api.populate_security_groups(security_groups)
        port_resource_requests = base_options.pop('port_resource_requests')
        instances_to_build = []
        image_cache = {}
        volumes = self._get_volumes_for_bdms(context, block_device_mapping)
        volume_az = self._validate_vol_az_for_create(base_options['availability_zone'], volumes.values())
        if volume_az:
            base_options['availability_zone'] = volume_az
        LOG.debug('Going to run %s instances...', num_instances)
        extra_specs = instance_type.extra_specs
        dp_name = extra_specs.get('accel:device_profile')
        dp_request_groups = []
        if dp_name:
            dp_request_groups = cyborg.get_device_profile_request_groups(context, dp_name)
        try:
            for i in range(num_instances):
                instance_uuid = uuidutils.generate_uuid()
                req_spec = objects.RequestSpec.from_components(context, instance_uuid, boot_meta, instance_type, base_options['numa_topology'], base_options['pci_requests'], filter_properties, instance_group, base_options['availability_zone'], security_groups=security_groups, port_resource_requests=port_resource_requests)
                if block_device_mapping:
                    root = block_device_mapping.root_bdm()
                    req_spec.is_bfv = bool(root and root.is_volume)
                else:
                    req_spec.is_bfv = False
                req_spec.num_instances = num_instances
                if network_metadata:
                    req_spec.network_metadata = network_metadata
                if destination:
                    req_spec.requested_destination = destination
                if dp_request_groups:
                    req_spec.requested_resources.extend(dp_request_groups)
                instance = objects.Instance(context=context)
                instance.uuid = instance_uuid
                instance.update(base_options)
                instance.keypairs = objects.KeyPairList(objects=[])
                if key_pair:
                    instance.keypairs.objects.append(key_pair)
                instance.trusted_certs = self._retrieve_trusted_certs_object(context, trusted_certs)
                instance = self.create_db_entry_for_new_instance(context, instance_type, boot_meta, instance, security_groups, block_device_mapping, num_instances, i, shutdown_terminate, create_instance=False)
                block_device_mapping = self._bdm_validate_set_size_and_instance(context, instance, instance_type, block_device_mapping, image_cache, volumes, supports_multiattach)
                instance_tags = self._transform_tags(tags, instance.uuid)
                build_request = objects.BuildRequest(context, instance=instance, instance_uuid=instance.uuid, project_id=instance.project_id, block_device_mappings=block_device_mapping, tags=instance_tags)
                inst_mapping = objects.InstanceMapping(context=context)
                inst_mapping.instance_uuid = instance_uuid
                inst_mapping.project_id = context.project_id
                inst_mapping.user_id = context.user_id
                inst_mapping.cell_mapping = None
                self._create_reqspec_buildreq_instmapping(context, req_spec, build_request, inst_mapping)
                instances_to_build.append((req_spec, build_request, inst_mapping))
                if instance_group:
                    if check_server_group_quota:
                        try:
                            objects.Quotas.check_deltas(context, {'server_group_members': 1}, instance_group, context.user_id)
                        except exception.OverQuota:
                            msg = _('Quota exceeded, too many servers in group')
                            raise exception.QuotaError(msg)
                    members = objects.InstanceGroup.add_members(context, instance_group.uuid, [instance.uuid])
                    if CONF.quota.recheck_quota and check_server_group_quota:
                        try:
                            objects.Quotas.check_deltas(context, {'server_group_members': 0}, instance_group, context.user_id)
                        except exception.OverQuota:
                            objects.InstanceGroup._remove_members_in_db(context, instance_group.id, [instance.uuid])
                            msg = _('Quota exceeded, too many servers in group')
                            raise exception.QuotaError(msg)
                    instance_group.members.extend(members)
        except Exception:
            with excutils.save_and_reraise_exception():
                self._cleanup_build_artifacts(None, instances_to_build)
        return instances_to_build

    @staticmethod
    def _retrieve_trusted_certs_object(context, trusted_certs, rebuild=False):
        """Convert user-requested trusted cert IDs to TrustedCerts object

        Also validates that the deployment is new enough to support trusted
        image certification validation.

        :param context: The user request auth context
        :param trusted_certs: list of user-specified trusted cert string IDs,
            may be None
        :param rebuild: True if rebuilding the server, False if creating a
            new server
        :returns: nova.objects.TrustedCerts object or None if no user-specified
            trusted cert IDs were given and nova is not configured with
            default trusted cert IDs
        """
        if trusted_certs:
            certs_to_return = objects.TrustedCerts(ids=trusted_certs)
        elif CONF.glance.verify_glance_signatures and CONF.glance.enable_certificate_validation and CONF.glance.default_trusted_certificate_ids:
            certs_to_return = objects.TrustedCerts(ids=CONF.glance.default_trusted_certificate_ids)
        else:
            return None
        return certs_to_return

    @staticmethod
    def _get_requested_instance_group(context, filter_properties):
        if not filter_properties or not filter_properties.get('scheduler_hints'):
            return
        group_hint = filter_properties.get('scheduler_hints').get('group')
        if not group_hint:
            return
        return objects.InstanceGroup.get_by_uuid(context, group_hint)

    def _create_instance(self, context, instance_type, image_href, kernel_id, ramdisk_id, min_count, max_count, display_name, display_description, key_name, key_data, security_groups, availability_zone, user_data, metadata, injected_files, admin_password, access_ip_v4, access_ip_v6, requested_networks, config_drive, block_device_mapping, auto_disk_config, filter_properties, reservation_id=None, legacy_bdm=True, shutdown_terminate=False, check_server_group_quota=False, tags=None, supports_multiattach=False, trusted_certs=None, supports_port_resource_request=False, requested_host=None, requested_hypervisor_hostname=None):
        """Verify all the input parameters regardless of the provisioning
        strategy being performed and schedule the instance(s) for
        creation.
        """
        if reservation_id is None:
            reservation_id = utils.generate_uid('r')
        security_groups = security_groups or ['default']
        min_count = min_count or 1
        max_count = max_count or min_count
        block_device_mapping = block_device_mapping or []
        tags = tags or []
        if image_href:
            (image_id, boot_meta) = self._get_image(context, image_href)
        else:
            if trusted_certs or (CONF.glance.verify_glance_signatures and CONF.glance.enable_certificate_validation and CONF.glance.default_trusted_certificate_ids):
                msg = _('Image certificate validation is not supported when booting from volume')
                raise exception.CertificateValidationFailed(message=msg)
            image_id = None
            boot_meta = block_device.get_bdm_image_metadata(context, self.image_api, self.volume_api, block_device_mapping, legacy_bdm)
        self._check_auto_disk_config(image=boot_meta, auto_disk_config=auto_disk_config)
        (base_options, max_net_count, key_pair, security_groups, network_metadata) = self._validate_and_build_base_options(context, instance_type, boot_meta, image_href, image_id, kernel_id, ramdisk_id, display_name, display_description, key_name, key_data, security_groups, availability_zone, user_data, metadata, access_ip_v4, access_ip_v6, requested_networks, config_drive, auto_disk_config, reservation_id, max_count, supports_port_resource_request)
        numa_topology = base_options.get('numa_topology')
        self._check_compute_service_for_mixed_instance(numa_topology)
        if max_net_count < min_count:
            raise exception.PortLimitExceeded()
        elif max_net_count < max_count:
            LOG.info('max count reduced from %(max_count)d to %(max_net_count)d due to network port quota', {'max_count': max_count, 'max_net_count': max_net_count})
            max_count = max_net_count
        block_device_mapping = self._check_and_transform_bdm(context, base_options, instance_type, boot_meta, min_count, max_count, block_device_mapping, legacy_bdm)
        self._checks_for_create_and_rebuild(context, image_id, boot_meta, instance_type, metadata, injected_files, block_device_mapping.root_bdm(), validate_numa=False)
        instance_group = self._get_requested_instance_group(context, filter_properties)
        tags = self._create_tag_list_obj(context, tags)
        instances_to_build = self._provision_instances(context, instance_type, min_count, max_count, base_options, boot_meta, security_groups, block_device_mapping, shutdown_terminate, instance_group, check_server_group_quota, filter_properties, key_pair, tags, trusted_certs, supports_multiattach, network_metadata, requested_host, requested_hypervisor_hostname)
        instances = []
        request_specs = []
        build_requests = []
        for (rs, build_request, im) in instances_to_build:
            build_requests.append(build_request)
            instance = build_request.get_new_instance(context)
            instances.append(instance)
            if requested_networks is not None:
                rs.requested_networks = requested_networks
            request_specs.append(rs)
        self.compute_task_api.schedule_and_build_instances(context, build_requests=build_requests, request_spec=request_specs, image=boot_meta, admin_password=admin_password, injected_files=injected_files, requested_networks=requested_networks, block_device_mapping=block_device_mapping, tags=tags)
        return (instances, reservation_id)

    @staticmethod
    def _cleanup_build_artifacts(instances, instances_to_build):
        for instance in instances or []:
            try:
                instance.destroy()
            except exception.InstanceNotFound:
                pass
        for (rs, build_request, im) in instances_to_build or []:
            try:
                rs.destroy()
            except exception.RequestSpecNotFound:
                pass
            try:
                build_request.destroy()
            except exception.BuildRequestNotFound:
                pass
            try:
                im.destroy()
            except exception.InstanceMappingNotFound:
                pass

    @staticmethod
    def _volume_size(instance_type, bdm):
        size = bdm.get('volume_size')
        if size is None and bdm.get('source_type') == 'blank' and (bdm.get('destination_type') == 'local'):
            if bdm.get('guest_format') == 'swap':
                size = instance_type.get('swap', 0)
            else:
                size = instance_type.get('ephemeral_gb', 0)
        return size

    def _prepare_image_mapping(self, instance_type, mappings):
        """Extract and format blank devices from image mappings."""
        prepared_mappings = []
        for bdm in block_device.mappings_prepend_dev(mappings):
            LOG.debug('Image bdm %s', bdm)
            virtual_name = bdm['virtual']
            if virtual_name == 'ami' or virtual_name == 'root':
                continue
            if not block_device.is_swap_or_ephemeral(virtual_name):
                continue
            guest_format = bdm.get('guest_format')
            if virtual_name == 'swap':
                guest_format = 'swap'
            if not guest_format:
                guest_format = CONF.default_ephemeral_format
            values = block_device.BlockDeviceDict({'device_name': bdm['device'], 'source_type': 'blank', 'destination_type': 'local', 'device_type': 'disk', 'guest_format': guest_format, 'delete_on_termination': True, 'boot_index': -1})
            values['volume_size'] = self._volume_size(instance_type, values)
            if values['volume_size'] == 0:
                continue
            prepared_mappings.append(values)
        return prepared_mappings

    def _bdm_validate_set_size_and_instance(self, context, instance, instance_type, block_device_mapping, image_cache, volumes, supports_multiattach=False):
        """Ensure the bdms are valid, then set size and associate with instance

        Because this method can be called multiple times when more than one
        instance is booted in a single request it makes a copy of the bdm list.

        :param context: nova auth RequestContext
        :param instance: Instance object
        :param instance_type: Flavor object - used for swap and ephemeral BDMs
        :param block_device_mapping: BlockDeviceMappingList object
        :param image_cache: dict of image dicts keyed by id which is used as a
            cache in case there are multiple BDMs in the same request using
            the same image to avoid redundant GET calls to the image service
        :param volumes: dict, keyed by volume id, of volume dicts from cinder
        :param supports_multiattach: True if the request supports multiattach
            volumes, False otherwise
        """
        LOG.debug('block_device_mapping %s', list(block_device_mapping), instance_uuid=instance.uuid)
        self._validate_bdm(context, instance, instance_type, block_device_mapping, image_cache, volumes, supports_multiattach)
        instance_block_device_mapping = block_device_mapping.obj_clone()
        for bdm in instance_block_device_mapping:
            bdm.volume_size = self._volume_size(instance_type, bdm)
            bdm.instance_uuid = instance.uuid
        return instance_block_device_mapping

    @staticmethod
    def _check_requested_volume_type(bdm, volume_type_id_or_name, volume_types):
        """If we are specifying a volume type, we need to get the
        volume type details from Cinder and make sure the ``volume_type``
        is available.
        """
        for vol_type in volume_types:
            if volume_type_id_or_name == vol_type['id'] or volume_type_id_or_name == vol_type['name']:
                bdm.volume_type = vol_type['name']
                break
        else:
            raise exception.VolumeTypeNotFound(id_or_name=volume_type_id_or_name)

    def _validate_bdm(self, context, instance, instance_type, block_device_mappings, image_cache, volumes, supports_multiattach=False):
        """Validate requested block device mappings.

        :param context: nova auth RequestContext
        :param instance: Instance object
        :param instance_type: Flavor object - used for swap and ephemeral BDMs
        :param block_device_mappings: BlockDeviceMappingList object
        :param image_cache: dict of image dicts keyed by id which is used as a
            cache in case there are multiple BDMs in the same request using
            the same image to avoid redundant GET calls to the image service
        :param volumes: dict, keyed by volume id, of volume dicts from cinder
        :param supports_multiattach: True if the request supports multiattach
            volumes, False otherwise
        """
        boot_indexes = sorted([bdm.boot_index for bdm in block_device_mappings if bdm.boot_index is not None and bdm.boot_index >= 0])
        if not boot_indexes or any((i != v for (i, v) in enumerate(boot_indexes))):
            LOG.debug('Invalid block device mapping boot sequence for instance: %s', list(block_device_mappings), instance=instance)
            raise exception.InvalidBDMBootSequence()
        volume_types = None
        for bdm in block_device_mappings:
            volume_type = bdm.volume_type
            if volume_type:
                if not volume_types:
                    volume_types = self.volume_api.get_all_volume_types(context)
                self._check_requested_volume_type(bdm, volume_type, volume_types)
            snapshot_id = bdm.snapshot_id
            volume_id = bdm.volume_id
            image_id = bdm.image_id
            if image_id is not None:
                if image_id != instance.get('image_ref') and image_id not in image_cache:
                    try:
                        image_cache[image_id] = self._get_image(context, image_id)
                    except Exception:
                        raise exception.InvalidBDMImage(id=image_id)
                if bdm.source_type == 'image' and bdm.destination_type == 'volume' and (not bdm.volume_size):
                    raise exception.InvalidBDM(message=_("Images with destination_type 'volume' need to have a non-zero size specified"))
            elif volume_id is not None:
                try:
                    volume = volumes[volume_id]
                    self._check_attach_and_reserve_volume(context, volume, instance, bdm, supports_multiattach, validate_az=False)
                    bdm.volume_size = volume.get('size')
                except (exception.CinderConnectionFailed, exception.InvalidVolume, exception.MultiattachNotSupportedOldMicroversion):
                    raise
                except exception.InvalidInput as exc:
                    raise exception.InvalidVolume(reason=exc.format_message())
                except Exception as e:
                    LOG.info('Failed validating volume %s. Error: %s', volume_id, e)
                    raise exception.InvalidBDMVolume(id=volume_id)
            elif snapshot_id is not None:
                try:
                    snap = self.volume_api.get_snapshot(context, snapshot_id)
                    bdm.volume_size = bdm.volume_size or snap.get('size')
                except exception.CinderConnectionFailed:
                    raise
                except Exception:
                    raise exception.InvalidBDMSnapshot(id=snapshot_id)
            elif bdm.source_type == 'blank' and bdm.destination_type == 'volume' and (not bdm.volume_size):
                raise exception.InvalidBDM(message=_("Blank volumes (source: 'blank', dest: 'volume') need to have non-zero size"))
            disk_bus = bdm.disk_bus if 'disk_bus' in bdm else None
            if disk_bus and disk_bus not in fields_obj.DiskBus.ALL:
                raise exception.InvalidBDMDiskBus(disk_bus=disk_bus)
        ephemeral_size = sum((bdm.volume_size or instance_type['ephemeral_gb'] for bdm in block_device_mappings if block_device.new_format_is_ephemeral(bdm)))
        if ephemeral_size > instance_type['ephemeral_gb']:
            raise exception.InvalidBDMEphemeralSize()
        swap_list = block_device.get_bdm_swap_list(block_device_mappings)
        if len(swap_list) > 1:
            msg = _('More than one swap drive requested.')
            raise exception.InvalidBDMFormat(details=msg)
        if swap_list:
            swap_size = swap_list[0].volume_size or 0
            if swap_size > instance_type['swap']:
                raise exception.InvalidBDMSwapSize()
        max_local = CONF.max_local_block_devices
        if max_local >= 0:
            num_local = len([bdm for bdm in block_device_mappings if bdm.destination_type == 'local'])
            if num_local > max_local:
                raise exception.InvalidBDMLocalsLimit()

    def _populate_instance_names(self, instance, num_instances, index):
        """Populate instance display_name and hostname.

        :param instance: The instance to set the display_name, hostname for
        :type instance: nova.objects.Instance
        :param num_instances: Total number of instances being created in this
            request
        :param index: The 0-based index of this particular instance
        """
        if 'display_name' not in instance or instance.display_name is None:
            instance.display_name = 'Server %s' % instance.uuid
        if num_instances == 1:
            default_hostname = 'Server-%s' % instance.uuid
            instance.hostname = utils.sanitize_hostname(instance.display_name, default_hostname)
        elif num_instances > 1:
            old_display_name = instance.display_name
            new_display_name = '%s-%d' % (old_display_name, index + 1)
            if utils.sanitize_hostname(old_display_name) == '':
                instance.hostname = 'Server-%s' % instance.uuid
            else:
                instance.hostname = utils.sanitize_hostname(new_display_name)
            instance.display_name = new_display_name

    def _populate_instance_for_create(self, context, instance, image, index, security_groups, instance_type, num_instances, shutdown_terminate):
        """Build the beginning of a new instance."""
        instance.launch_index = index
        instance.vm_state = vm_states.BUILDING
        instance.task_state = task_states.SCHEDULING
        info_cache = objects.InstanceInfoCache()
        info_cache.instance_uuid = instance.uuid
        info_cache.network_info = network_model.NetworkInfo()
        instance.info_cache = info_cache
        instance.flavor = instance_type
        instance.old_flavor = None
        instance.new_flavor = None
        if CONF.ephemeral_storage_encryption.enabled:
            cipher = CONF.ephemeral_storage_encryption.cipher
            algorithm = cipher.split('-')[0] if cipher else None
            instance.ephemeral_key_uuid = self.key_manager.create_key(context, algorithm=algorithm, length=CONF.ephemeral_storage_encryption.key_size)
        else:
            instance.ephemeral_key_uuid = None
        if not instance.obj_attr_is_set('system_metadata'):
            instance.system_metadata = {}
        instance.system_metadata = utils.instance_sys_meta(instance)
        system_meta = utils.get_system_metadata_from_image(image, instance_type)
        system_meta.setdefault('image_base_image_ref', instance.image_ref)
        system_meta['owner_user_name'] = context.user_name
        system_meta['owner_project_name'] = context.project_name
        instance.system_metadata.update(system_meta)
        instance.security_groups = objects.SecurityGroupList()
        self._populate_instance_names(instance, num_instances, index)
        instance.shutdown_terminate = shutdown_terminate
        return instance

    def _create_tag_list_obj(self, context, tags):
        """Create TagList objects from simple string tags.

        :param context: security context.
        :param tags: simple string tags from API request.
        :returns: TagList object.
        """
        tag_list = [objects.Tag(context=context, tag=t) for t in tags]
        tag_list_obj = objects.TagList(objects=tag_list)
        return tag_list_obj

    def _transform_tags(self, tags, resource_id):
        """Change the resource_id of the tags according to the input param.

        Because this method can be called multiple times when more than one
        instance is booted in a single request it makes a copy of the tags
        list.

        :param tags: TagList object.
        :param resource_id: string.
        :returns: TagList object.
        """
        instance_tags = tags.obj_clone()
        for tag in instance_tags:
            tag.resource_id = resource_id
        return instance_tags

    def create_db_entry_for_new_instance(self, context, instance_type, image, instance, security_group, block_device_mapping, num_instances, index, shutdown_terminate=False, create_instance=True):
        """Create an entry in the DB for this new instance,
        including any related table updates (such as security group,
        etc).

        This is called by the scheduler after a location for the
        instance has been determined.

        :param create_instance: Determines if the instance is created here or
            just populated for later creation. This is done so that this code
            can be shared with cellsv1 which needs the instance creation to
            happen here. It should be removed and this method cleaned up when
            cellsv1 is a distant memory.
        """
        self._populate_instance_for_create(context, instance, image, index, security_group, instance_type, num_instances, shutdown_terminate)
        if create_instance:
            instance.create()
        return instance

    def _check_multiple_instances_with_neutron_ports(self, requested_networks):
        """Check whether multiple instances are created from port id(s)."""
        for requested_net in requested_networks:
            if requested_net.port_id:
                msg = _('Unable to launch multiple instances with a single configured port ID. Please launch your instance one by one with different ports.')
                raise exception.MultiplePortsNotApplicable(reason=msg)

    def _check_multiple_instances_with_specified_ip(self, requested_networks):
        """Check whether multiple instances are created with specified ip."""
        for requested_net in requested_networks:
            if requested_net.network_id and requested_net.address:
                msg = _('max_count cannot be greater than 1 if an fixed_ip is specified.')
                raise exception.InvalidFixedIpAndMaxCountRequest(reason=msg)

    def create(self, context, instance_type, image_href, kernel_id=None, ramdisk_id=None, min_count=None, max_count=None, display_name=None, display_description=None, key_name=None, key_data=None, security_groups=None, availability_zone=None, forced_host=None, forced_node=None, user_data=None, metadata=None, injected_files=None, admin_password=None, block_device_mapping=None, access_ip_v4=None, access_ip_v6=None, requested_networks=None, config_drive=None, auto_disk_config=None, scheduler_hints=None, legacy_bdm=True, shutdown_terminate=False, check_server_group_quota=False, tags=None, supports_multiattach=False, trusted_certs=None, supports_port_resource_request=False, requested_host=None, requested_hypervisor_hostname=None):
        """Provision instances, sending instance information to the
        scheduler.  The scheduler will determine where the instance(s)
        go and will handle creating the DB entries.

        Returns a tuple of (instances, reservation_id)
        """
        if requested_networks and max_count is not None and (max_count > 1):
            self._check_multiple_instances_with_specified_ip(requested_networks)
            self._check_multiple_instances_with_neutron_ports(requested_networks)
        if availability_zone and forced_host is None:
            azs = availability_zones.get_availability_zones(context.elevated(), self.host_api, get_only_available=True)
            if availability_zone not in azs:
                msg = _('The requested availability zone is not available')
                raise exception.InvalidRequest(msg)
        filter_properties = scheduler_utils.build_filter_properties(scheduler_hints, forced_host, forced_node, instance_type)
        return self._create_instance(context, instance_type, image_href, kernel_id, ramdisk_id, min_count, max_count, display_name, display_description, key_name, key_data, security_groups, availability_zone, user_data, metadata, injected_files, admin_password, access_ip_v4, access_ip_v6, requested_networks, config_drive, block_device_mapping, auto_disk_config, filter_properties=filter_properties, legacy_bdm=legacy_bdm, shutdown_terminate=shutdown_terminate, check_server_group_quota=check_server_group_quota, tags=tags, supports_multiattach=supports_multiattach, trusted_certs=trusted_certs, supports_port_resource_request=supports_port_resource_request, requested_host=requested_host, requested_hypervisor_hostname=requested_hypervisor_hostname)

    def _check_auto_disk_config(self, instance=None, image=None, auto_disk_config=None):
        if auto_disk_config is None:
            return
        if not image and (not instance):
            return
        if image:
            image_props = image.get('properties', {})
            auto_disk_config_img = utils.get_auto_disk_config_from_image_props(image_props)
            image_ref = image.get('id')
        else:
            sys_meta = utils.instance_sys_meta(instance)
            image_ref = sys_meta.get('image_base_image_ref')
            auto_disk_config_img = utils.get_auto_disk_config_from_instance(sys_meta=sys_meta)
        self._ensure_auto_disk_config_is_valid(auto_disk_config_img, auto_disk_config, image_ref)

    def _lookup_instance(self, context, uuid):
        """Helper method for pulling an instance object from a database.

        During the transition to cellsv2 there is some complexity around
        retrieving an instance from the database which this method hides. If
        there is an instance mapping then query the cell for the instance, if
        no mapping exists then query the configured nova database.

        Once we are past the point that all deployments can be assumed to be
        migrated to cellsv2 this method can go away.
        """
        inst_map = None
        try:
            inst_map = objects.InstanceMapping.get_by_instance_uuid(context, uuid)
        except exception.InstanceMappingNotFound:
            pass
        if inst_map is None or inst_map.cell_mapping is None:
            cell = None
            try:
                instance = objects.Instance.get_by_uuid(context, uuid)
            except exception.InstanceNotFound:
                return (None, None)
        else:
            cell = inst_map.cell_mapping
            with nova_context.target_cell(context, cell) as cctxt:
                try:
                    instance = objects.Instance.get_by_uuid(cctxt, uuid)
                except exception.InstanceNotFound:
                    return (None, None)
        return (cell, instance)

    def _delete_while_booting(self, context, instance):
        """Handle deletion if the instance has not reached a cell yet

        Deletion before an instance reaches a cell needs to be handled
        differently. What we're attempting to do is delete the BuildRequest
        before the api level conductor does.  If we succeed here then the boot
        request stops before reaching a cell.  If not then the instance will
        need to be looked up in a cell db and the normal delete path taken.
        """
        deleted = self._attempt_delete_of_buildrequest(context, instance)
        if deleted:
            try:
                instance_uuid = instance.uuid
                (cell, instance) = self._lookup_instance(context, instance_uuid)
                if instance is not None:
                    if cell:
                        with nova_context.target_cell(context, cell) as cctxt:
                            with compute_utils.notify_about_instance_delete(self.notifier, cctxt, instance):
                                instance.destroy()
                    else:
                        instance.destroy()
            except exception.InstanceNotFound:
                pass
            return True
        return False

    def _local_delete_cleanup(self, context, instance_uuid):
        try:
            self.placementclient.delete_allocation_for_instance(context, instance_uuid)
        except exception.AllocationDeleteFailed:
            LOG.info('Allocation delete failed during local delete cleanup.', instance_uuid=instance_uuid)
        try:
            self._update_queued_for_deletion(context, instance_uuid, True)
        except exception.InstanceMappingNotFound:
            LOG.info('Instance Mapping does not exist while attempting local delete cleanup.', instance_uuid=instance_uuid)

    def _attempt_delete_of_buildrequest(self, context, instance):
        try:
            build_req = objects.BuildRequest.get_by_instance_uuid(context, instance.uuid)
            build_req.destroy()
        except exception.BuildRequestNotFound:
            return False
        self._local_cleanup_bdm_volumes(build_req.block_device_mappings, instance, context)
        return True

    def _delete(self, context, instance, delete_type, cb, **instance_attrs):
        if instance.disable_terminate:
            LOG.info('instance termination disabled', instance=instance)
            return
        cell = None
        may_have_ports_or_volumes = compute_utils.may_have_ports_or_volumes(instance)
        if not instance.host and (not may_have_ports_or_volumes):
            try:
                if self._delete_while_booting(context, instance):
                    self._local_delete_cleanup(context, instance.uuid)
                    return
                instance_uuid = instance.uuid
                (cell, instance) = self._lookup_instance(context, instance.uuid)
                if cell and instance:
                    try:
                        with compute_utils.notify_about_instance_delete(self.notifier, context, instance):
                            instance.destroy()
                    except exception.InstanceNotFound:
                        pass
                    self._local_delete_cleanup(context, instance.uuid)
                    return
                if not instance:
                    self._local_delete_cleanup(context, instance_uuid)
                    return
            except exception.ObjectActionError:
                (cell, instance) = self._lookup_instance(context, instance.uuid)
                if not instance:
                    self._local_delete_cleanup(context, instance_uuid)
                    return
        bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(context, instance.uuid)
        if instance.vm_state in (vm_states.SHELVED, vm_states.SHELVED_OFFLOADED):
            snapshot_id = instance.system_metadata.get('shelved_image_id')
            LOG.info('Working on deleting snapshot %s from shelved instance...', snapshot_id, instance=instance)
            try:
                self.image_api.delete(context, snapshot_id)
            except (exception.ImageNotFound, exception.ImageNotAuthorized) as exc:
                LOG.warning('Failed to delete snapshot from shelved instance (%s).', exc.format_message(), instance=instance)
            except Exception:
                LOG.exception('Something wrong happened when trying to delete snapshot from shelved instance.', instance=instance)
        original_task_state = instance.task_state
        try:
            instance.update(instance_attrs)
            instance.progress = 0
            instance.save()
            if not instance.host and (not may_have_ports_or_volumes):
                try:
                    with compute_utils.notify_about_instance_delete(self.notifier, context, instance, delete_type if delete_type != 'soft_delete' else 'delete'):
                        instance.destroy()
                    LOG.info('Instance deleted and does not have host field, its vm_state is %(state)s.', {'state': instance.vm_state}, instance=instance)
                    self._local_delete_cleanup(context, instance.uuid)
                    return
                except exception.ObjectActionError as ex:
                    LOG.debug('Refreshing instance because: %s', ex, instance=instance)
                    instance.refresh()
            if instance.vm_state == vm_states.RESIZED:
                self._confirm_resize_on_deleting(context, instance)
                if delete_type == 'soft_delete':
                    instance.task_state = instance_attrs['task_state']
                    instance.save()
            is_local_delete = True
            try:
                if instance.host is not None:
                    service = objects.Service.get_by_compute_host(context.elevated(), instance.host)
                    is_local_delete = not self.servicegroup_api.service_is_up(service)
                if not is_local_delete:
                    if original_task_state in (task_states.DELETING, task_states.SOFT_DELETING):
                        LOG.info('Instance is already in deleting state, ignoring this request', instance=instance)
                        return
                    self._record_action_start(context, instance, instance_actions.DELETE)
                    cb(context, instance, bdms)
            except exception.ComputeHostNotFound:
                LOG.debug('Compute host %s not found during service up check, going to local delete instance', instance.host, instance=instance)
            if is_local_delete:
                if cell is None:
                    try:
                        im = objects.InstanceMapping.get_by_instance_uuid(context, instance.uuid)
                        cell = im.cell_mapping
                    except exception.InstanceMappingNotFound:
                        LOG.warning('During local delete, failed to find instance mapping', instance=instance)
                        return
                LOG.debug('Doing local delete in cell %s', cell.identity, instance=instance)
                with nova_context.target_cell(context, cell) as cctxt:
                    self._local_delete(cctxt, instance, bdms, delete_type, cb)
        except exception.InstanceNotFound:
            pass

    def _confirm_resize_on_deleting(self, context, instance):
        migration = None
        for status in ('finished', 'confirming'):
            try:
                migration = objects.Migration.get_by_instance_and_status(context.elevated(), instance.uuid, status)
                LOG.info('Found an unconfirmed migration during delete, id: %(id)s, status: %(status)s', {'id': migration.id, 'status': migration.status}, instance=instance)
                break
            except exception.MigrationNotFoundByStatus:
                pass
        if not migration:
            LOG.info('Instance may have been confirmed during delete', instance=instance)
            return
        self._record_action_start(context, instance, instance_actions.CONFIRM_RESIZE)
        if migration.cross_cell_move:
            self.compute_task_api.confirm_snapshot_based_resize(context, instance, migration, do_cast=False)
        else:
            self.compute_rpcapi.confirm_resize(context, instance, migration, migration.source_compute, cast=False)

    def _local_cleanup_bdm_volumes(self, bdms, instance, context):
        """The method deletes the bdm records and, if a bdm is a volume, call
        the terminate connection and the detach volume via the Volume API.
        """
        elevated = context.elevated()
        for bdm in bdms:
            if bdm.is_volume:
                try:
                    if bdm.attachment_id:
                        self.volume_api.attachment_delete(context, bdm.attachment_id)
                    else:
                        connector = compute_utils.get_stashed_volume_connector(bdm, instance)
                        if connector:
                            self.volume_api.terminate_connection(context, bdm.volume_id, connector)
                        else:
                            LOG.debug('Unable to find connector for volume %s, not attempting terminate_connection.', bdm.volume_id, instance=instance)
                        self.volume_api.detach(elevated, bdm.volume_id, instance.uuid)
                    if bdm.delete_on_termination:
                        self.volume_api.delete(context, bdm.volume_id)
                except Exception as exc:
                    LOG.warning('Ignoring volume cleanup failure due to %s', exc, instance=instance)
            if 'id' in bdm:
                bdm.destroy()

    @property
    def placementclient(self):
        if self._placementclient is None:
            self._placementclient = report.SchedulerReportClient()
        return self._placementclient

    def _local_delete(self, context, instance, bdms, delete_type, cb):
        if instance.vm_state == vm_states.SHELVED_OFFLOADED:
            LOG.info("instance is in SHELVED_OFFLOADED state, cleanup the instance's info from database.", instance=instance)
        else:
            LOG.warning("instance's host %s is down, deleting from database", instance.host, instance=instance)
        with compute_utils.notify_about_instance_delete(self.notifier, context, instance, delete_type if delete_type != 'soft_delete' else 'delete'):
            elevated = context.elevated()
            self.network_api.deallocate_for_instance(elevated, instance)
            self._local_cleanup_bdm_volumes(bdms, instance, context)
            compute_utils.delete_arqs_if_needed(context, instance)
            self.placementclient.delete_allocation_for_instance(context, instance.uuid)
            cb(context, instance, bdms, local=True)
            instance.destroy()

    @staticmethod
    def _update_queued_for_deletion(context, instance_uuid, qfd):
        im = objects.InstanceMapping.get_by_instance_uuid(context, instance_uuid)
        im.queued_for_delete = qfd
        im.save()

    def _do_delete(self, context, instance, bdms, local=False):
        if local:
            instance.vm_state = vm_states.DELETED
            instance.task_state = None
            instance.terminated_at = timeutils.utcnow()
            instance.save()
        else:
            self.compute_rpcapi.terminate_instance(context, instance, bdms)
        self._update_queued_for_deletion(context, instance.uuid, True)

    def _do_soft_delete(self, context, instance, bdms, local=False):
        if local:
            instance.vm_state = vm_states.SOFT_DELETED
            instance.task_state = None
            instance.terminated_at = timeutils.utcnow()
            instance.save()
        else:
            self.compute_rpcapi.soft_delete_instance(context, instance)
        self._update_queued_for_deletion(context, instance.uuid, True)

    @check_instance_lock
    @check_instance_state(vm_state=None, task_state=None, must_have_launched=True)
    def soft_delete(self, context, instance):
        """Terminate an instance."""
        LOG.debug('Going to try to soft delete instance', instance=instance)
        self._delete(context, instance, 'soft_delete', self._do_soft_delete, task_state=task_states.SOFT_DELETING, deleted_at=timeutils.utcnow())

    def _delete_instance(self, context, instance):
        self._delete(context, instance, 'delete', self._do_delete, task_state=task_states.DELETING)

    @check_instance_lock
    @check_instance_state(vm_state=None, task_state=None, must_have_launched=False)
    def delete(self, context, instance):
        """Terminate an instance."""
        LOG.debug('Going to try to terminate instance', instance=instance)
        self._delete_instance(context, instance)

    @check_instance_lock
    @check_instance_state(vm_state=[vm_states.SOFT_DELETED])
    def restore(self, context, instance):
        """Restore a previously deleted (but not reclaimed) instance."""
        flavor = instance.get_flavor()
        (project_id, user_id) = quotas_obj.ids_from_instance(context, instance)
        compute_utils.check_num_instances_quota(context, flavor, 1, 1, project_id=project_id, user_id=user_id)
        self._record_action_start(context, instance, instance_actions.RESTORE)
        if instance.host:
            instance.task_state = task_states.RESTORING
            instance.deleted_at = None
            instance.save(expected_task_state=[None])
            self.compute_rpcapi.restore_instance(context, instance)
        else:
            instance.vm_state = vm_states.ACTIVE
            instance.task_state = None
            instance.deleted_at = None
            instance.save(expected_task_state=[None])
        self._update_queued_for_deletion(context, instance.uuid, False)

    @check_instance_lock
    @check_instance_state(task_state=None, must_have_launched=False)
    def force_delete(self, context, instance):
        """Force delete an instance in any vm_state/task_state."""
        self._delete(context, instance, 'force_delete', self._do_delete, task_state=task_states.DELETING)

    def force_stop(self, context, instance, do_cast=True, clean_shutdown=True):
        LOG.debug('Going to try to stop instance', instance=instance)
        instance.task_state = task_states.POWERING_OFF
        instance.progress = 0
        instance.save(expected_task_state=[None])
        self._record_action_start(context, instance, instance_actions.STOP)
        self.compute_rpcapi.stop_instance(context, instance, do_cast=do_cast, clean_shutdown=clean_shutdown)

    @check_instance_lock
    @check_instance_host()
    @check_instance_state(vm_state=[vm_states.ACTIVE, vm_states.ERROR])
    def stop(self, context, instance, do_cast=True, clean_shutdown=True):
        """Stop an instance."""
        self.force_stop(context, instance, do_cast, clean_shutdown)

    @check_instance_lock
    @check_instance_host()
    @check_instance_state(vm_state=[vm_states.STOPPED])
    def start(self, context, instance):
        """Start an instance."""
        LOG.debug('Going to try to start instance', instance=instance)
        instance.task_state = task_states.POWERING_ON
        instance.save(expected_task_state=[None])
        self._record_action_start(context, instance, instance_actions.START)
        self.compute_rpcapi.start_instance(context, instance)

    @check_instance_lock
    @check_instance_host()
    @check_instance_state(vm_state=vm_states.ALLOW_TRIGGER_CRASH_DUMP)
    def trigger_crash_dump(self, context, instance):
        """Trigger crash dump in an instance."""
        LOG.debug('Try to trigger crash dump', instance=instance)
        self._record_action_start(context, instance, instance_actions.TRIGGER_CRASH_DUMP)
        self.compute_rpcapi.trigger_crash_dump(context, instance)

    def _generate_minimal_construct_for_down_cells(self, context, down_cell_uuids, project, limit):
        """Generate a list of minimal instance constructs for a given list of
        cells that did not respond to a list operation. This will list
        every instance mapping in the affected cells and return a minimal
        objects.Instance for each (non-queued-for-delete) mapping.

        :param context: RequestContext
        :param down_cell_uuids: A list of cell UUIDs that did not respond
        :param project: A project ID to filter mappings, or None
        :param limit: A numeric limit on the number of results, or None
        :returns: An InstanceList() of partial Instance() objects
        """
        unavailable_servers = objects.InstanceList()
        for cell_uuid in down_cell_uuids:
            LOG.warning('Cell %s is not responding and hence only partial results are available from this cell if any.', cell_uuid)
            instance_mappings = objects.InstanceMappingList.get_not_deleted_by_cell_and_project(context, cell_uuid, project, limit=limit)
            for im in instance_mappings:
                unavailable_servers.objects.append(objects.Instance(context=context, uuid=im.instance_uuid, project_id=im.project_id, created_at=im.created_at))
            if limit is not None:
                limit -= len(instance_mappings)
                if limit <= 0:
                    break
        return unavailable_servers

    def _get_instance_map_or_none(self, context, instance_uuid):
        try:
            inst_map = objects.InstanceMapping.get_by_instance_uuid(context, instance_uuid)
        except exception.InstanceMappingNotFound:
            inst_map = None
        return inst_map

    @staticmethod
    def _save_user_id_in_instance_mapping(mapping, instance):
        if 'user_id' not in mapping and instance.user_id is not None:
            mapping.user_id = instance.user_id
            mapping.save()

    def _get_instance_from_cell(self, context, im, expected_attrs, cell_down_support):
        nova_context.set_target_cell(context, im.cell_mapping)
        uuid = im.instance_uuid
        result = nova_context.scatter_gather_single_cell(context, im.cell_mapping, objects.Instance.get_by_uuid, uuid, expected_attrs=expected_attrs)
        cell_uuid = im.cell_mapping.uuid
        if not nova_context.is_cell_failure_sentinel(result[cell_uuid]):
            inst = result[cell_uuid]
            self._save_user_id_in_instance_mapping(im, inst)
            return inst
        elif isinstance(result[cell_uuid], exception.InstanceNotFound):
            raise exception.InstanceNotFound(instance_id=uuid)
        elif cell_down_support:
            if im.queued_for_delete:
                raise exception.InstanceNotFound(instance_id=uuid)
            LOG.warning('Cell %s is not responding and hence only partial results are available from this cell.', cell_uuid)
            try:
                rs = objects.RequestSpec.get_by_instance_uuid(context, uuid)
                image_ref = rs.image.id if rs.image and 'id' in rs.image else None
                inst = objects.Instance(context=context, power_state=0, uuid=uuid, project_id=im.project_id, created_at=im.created_at, user_id=rs.user_id, flavor=rs.flavor, image_ref=image_ref, availability_zone=rs.availability_zone)
                self._save_user_id_in_instance_mapping(im, inst)
                return inst
            except exception.RequestSpecNotFound:
                raise exception.InstanceNotFound(instance_id=uuid)
        else:
            raise exception.NovaException(_('Cell %s is not responding and hence instance info is not available.') % cell_uuid)

    def _get_instance(self, context, instance_uuid, expected_attrs, cell_down_support=False):
        inst_map = self._get_instance_map_or_none(context, instance_uuid)
        if inst_map and inst_map.cell_mapping is not None:
            instance = self._get_instance_from_cell(context, inst_map, expected_attrs, cell_down_support)
        elif inst_map and inst_map.cell_mapping is None:
            try:
                build_req = objects.BuildRequest.get_by_instance_uuid(context, instance_uuid)
                instance = build_req.instance
            except exception.BuildRequestNotFound:
                inst_map = self._get_instance_map_or_none(context, instance_uuid)
                if inst_map and inst_map.cell_mapping is not None:
                    instance = self._get_instance_from_cell(context, inst_map, expected_attrs, cell_down_support)
                else:
                    raise exception.InstanceNotFound(instance_id=instance_uuid)
        else:
            raise exception.InstanceNotFound(instance_id=instance_uuid)
        return instance

    def get(self, context, instance_id, expected_attrs=None, cell_down_support=False):
        """Get a single instance with the given instance_id.

        :param cell_down_support: True if the API (and caller) support
                                  returning a minimal instance
                                  construct if the relevant cell is
                                  down. If False, an error is raised
                                  since the instance cannot be retrieved
                                  due to the cell being down.
        """
        if not expected_attrs:
            expected_attrs = []
        expected_attrs.extend(['metadata', 'system_metadata', 'security_groups', 'info_cache'])
        try:
            if uuidutils.is_uuid_like(instance_id):
                LOG.debug('Fetching instance by UUID', instance_uuid=instance_id)
                instance = self._get_instance(context, instance_id, expected_attrs, cell_down_support=cell_down_support)
            else:
                LOG.debug('Failed to fetch instance by id %s', instance_id)
                raise exception.InstanceNotFound(instance_id=instance_id)
        except exception.InvalidID:
            LOG.debug('Invalid instance id %s', instance_id)
            raise exception.InstanceNotFound(instance_id=instance_id)
        return instance

    def get_all(self, context, search_opts=None, limit=None, marker=None, expected_attrs=None, sort_keys=None, sort_dirs=None, cell_down_support=False, all_tenants=False):
        """Get all instances filtered by one of the given parameters.

        If there is no filter and the context is an admin, it will retrieve
        all instances in the system.

        Deleted instances will be returned by default, unless there is a
        search option that says otherwise.

        The results will be sorted based on the list of sort keys in the
        'sort_keys' parameter (first value is primary sort key, second value is
        secondary sort ket, etc.). For each sort key, the associated sort
        direction is based on the list of sort directions in the 'sort_dirs'
        parameter.

        :param cell_down_support: True if the API (and caller) support
                                  returning a minimal instance
                                  construct if the relevant cell is
                                  down. If False, instances from
                                  unreachable cells will be omitted.
        :param all_tenants: True if the "all_tenants" filter was passed.

        """
        if search_opts is None:
            search_opts = {}
        LOG.debug('Searching by: %s', str(search_opts))
        filters = {}

        def _remap_flavor_filter(flavor_id):
            flavor = objects.Flavor.get_by_flavor_id(context, flavor_id)
            filters['instance_type_id'] = flavor.id

        def _remap_fixed_ip_filter(fixed_ip):
            filters['ip'] = '^%s$' % fixed_ip.replace('.', '\\.')
        filter_mapping = {'image': 'image_ref', 'name': 'display_name', 'tenant_id': 'project_id', 'flavor': _remap_flavor_filter, 'fixed_ip': _remap_fixed_ip_filter}
        for (opt, value) in search_opts.items():
            try:
                remap_object = filter_mapping[opt]
            except KeyError:
                filters[opt] = value
            else:
                if isinstance(remap_object, str):
                    filters[remap_object] = value
                else:
                    try:
                        remap_object(value)
                    except ValueError:
                        return objects.InstanceList()
        filter_ip = 'ip6' in filters or 'ip' in filters
        skip_build_request = False
        orig_limit = limit
        if filter_ip:
            skip_build_request = marker is None
            if self.network_api.has_substr_port_filtering_extension(context):
                filter_ip = False
                instance_uuids = self._ip_filter_using_neutron(context, filters)
                if instance_uuids:
                    if 'uuid' in filters and filters['uuid']:
                        filter_uuids = filters['uuid']
                        if isinstance(filter_uuids, list):
                            instance_uuids.extend(filter_uuids)
                        elif filter_uuids not in instance_uuids:
                            instance_uuids.append(filter_uuids)
                    filters['uuid'] = instance_uuids
                else:
                    return objects.InstanceList()
            elif limit:
                LOG.debug('Removing limit for DB query due to IP filter')
                limit = None
        if skip_build_request:
            build_requests = objects.BuildRequestList()
        else:
            try:
                build_requests = objects.BuildRequestList.get_by_filters(context, filters, limit=limit, marker=marker, sort_keys=sort_keys, sort_dirs=sort_dirs)
                marker = None
            except exception.MarkerNotFound:
                build_requests = objects.BuildRequestList()
        build_req_instances = objects.InstanceList(objects=[build_req.instance for build_req in build_requests])
        limit = limit - len(build_req_instances) if limit else limit
        fields = ['metadata', 'info_cache', 'security_groups']
        if expected_attrs:
            fields.extend(expected_attrs)
        (insts, down_cell_uuids) = instance_list.get_instance_objects_sorted(context, filters, limit, marker, fields, sort_keys, sort_dirs, cell_down_support=cell_down_support)

        def _get_unique_filter_method():
            seen_uuids = set()

            def _filter(instance):
                if instance.uuid in seen_uuids:
                    return False
                seen_uuids.add(instance.uuid)
                return True
            return _filter
        filter_method = _get_unique_filter_method()
        limit = limit - len(insts) if limit else limit
        instances = objects.InstanceList(objects=list(filter(filter_method, build_req_instances.objects + insts.objects)))
        if filter_ip:
            instances = self._ip_filter(instances, filters, orig_limit)
        if cell_down_support:
            project = search_opts.get('project_id', context.project_id)
            if all_tenants:
                project = None
            limit = orig_limit - len(instances) if limit else limit
            return self._generate_minimal_construct_for_down_cells(context, down_cell_uuids, project, limit) + instances
        return instances

    @staticmethod
    def _ip_filter(inst_models, filters, limit):
        ipv4_f = re.compile(str(filters.get('ip')))
        ipv6_f = re.compile(str(filters.get('ip6')))

        def _match_instance(instance):
            nw_info = instance.get_network_info()
            for vif in nw_info:
                for fixed_ip in vif.fixed_ips():
                    address = fixed_ip.get('address')
                    if not address:
                        continue
                    version = fixed_ip.get('version')
                    if version == 4 and ipv4_f.match(address) or (version == 6 and ipv6_f.match(address)):
                        return True
            return False
        result_objs = []
        for instance in inst_models:
            if _match_instance(instance):
                result_objs.append(instance)
                if limit and len(result_objs) == limit:
                    break
        return objects.InstanceList(objects=result_objs)

    def _ip_filter_using_neutron(self, context, filters):
        ip4_address = filters.get('ip')
        ip6_address = filters.get('ip6')
        addresses = [ip4_address, ip6_address]
        uuids = []
        for address in addresses:
            if address:
                try:
                    ports = self.network_api.list_ports(context, fixed_ips='ip_address_substr=' + address, fields=['device_id'])['ports']
                    for port in ports:
                        uuids.append(port['device_id'])
                except Exception as e:
                    LOG.error('An error occurred while listing ports with an ip_address filter value of "%s". Error: %s', address, str(e))
        return uuids

    def update_instance(self, context, instance, updates):
        """Updates a single Instance object with some updates dict.

        Returns the updated instance.
        """
        if instance.obj_attr_is_set('id'):
            instance.update(updates)
            instance.save()
        else:
            try:
                build_req = objects.BuildRequest.get_by_instance_uuid(context, instance.uuid)
                instance = build_req.instance
                instance.update(updates)
                build_req.save()
            except exception.BuildRequestNotFound:
                expected_attrs = ['flavor', 'pci_devices', 'numa_topology', 'tags', 'metadata', 'system_metadata', 'security_groups', 'info_cache']
                inst_map = self._get_instance_map_or_none(context, instance.uuid)
                if inst_map and inst_map.cell_mapping is not None:
                    with nova_context.target_cell(context, inst_map.cell_mapping) as cctxt:
                        instance = objects.Instance.get_by_uuid(cctxt, instance.uuid, expected_attrs=expected_attrs)
                        instance.update(updates)
                        instance.save()
                else:
                    raise exception.InstanceNotFound(instance_id=instance.uuid)
        return instance

    @check_instance_state(vm_state=[vm_states.ACTIVE, vm_states.STOPPED, vm_states.PAUSED, vm_states.SUSPENDED])
    def backup(self, context, instance, name, backup_type, rotation, extra_properties=None):
        """Backup the given instance

        :param instance: nova.objects.instance.Instance object
        :param name: name of the backup
        :param backup_type: 'daily' or 'weekly'
        :param rotation: int representing how many backups to keep around;
            None if rotation shouldn't be used (as in the case of snapshots)
        :param extra_properties: dict of extra image properties to include
                                 when creating the image.
        :returns: A dict containing image metadata
        """
        props_copy = dict(extra_properties, backup_type=backup_type)
        if compute_utils.is_volume_backed_instance(context, instance):
            LOG.info("It's not supported to backup volume backed instance.", instance=instance)
            raise exception.InvalidRequest(_('Backup is not supported for volume-backed instances.'))
        else:
            image_meta = compute_utils.create_image(context, instance, name, 'backup', self.image_api, extra_properties=props_copy)
        instance.task_state = task_states.IMAGE_BACKUP
        instance.save(expected_task_state=[None])
        self._record_action_start(context, instance, instance_actions.BACKUP)
        self.compute_rpcapi.backup_instance(context, instance, image_meta['id'], backup_type, rotation)
        return image_meta

    @check_instance_state(vm_state=[vm_states.ACTIVE, vm_states.STOPPED, vm_states.PAUSED, vm_states.SUSPENDED])
    def snapshot(self, context, instance, name, extra_properties=None):
        """Snapshot the given instance.

        :param instance: nova.objects.instance.Instance object
        :param name: name of the snapshot
        :param extra_properties: dict of extra image properties to include
                                 when creating the image.
        :returns: A dict containing image metadata
        """
        image_meta = compute_utils.create_image(context, instance, name, 'snapshot', self.image_api, extra_properties=extra_properties)
        instance.task_state = task_states.IMAGE_SNAPSHOT_PENDING
        try:
            instance.save(expected_task_state=[None])
        except (exception.InstanceNotFound, exception.UnexpectedDeletingTaskStateError) as ex:
            LOG.debug('Instance disappeared during snapshot.', instance=instance)
            try:
                image_id = image_meta['id']
                self.image_api.delete(context, image_id)
                LOG.info('Image %s deleted because instance deleted before snapshot started.', image_id, instance=instance)
            except exception.ImageNotFound:
                pass
            except Exception as exc:
                LOG.warning('Error while trying to clean up image %(img_id)s: %(error_msg)s', {'img_id': image_meta['id'], 'error_msg': str(exc)})
            attr = 'task_state'
            state = task_states.DELETING
            if type(ex) == exception.InstanceNotFound:
                attr = 'vm_state'
                state = vm_states.DELETED
            raise exception.InstanceInvalidState(attr=attr, instance_uuid=instance.uuid, state=state, method='snapshot')
        self._record_action_start(context, instance, instance_actions.CREATE_IMAGE)
        self.compute_rpcapi.snapshot_instance(context, instance, image_meta['id'])
        return image_meta

    @check_instance_state(vm_state=[vm_states.ACTIVE, vm_states.STOPPED, vm_states.PAUSED, vm_states.SUSPENDED])
    def snapshot_volume_backed(self, context, instance, name, extra_properties=None):
        """Snapshot the given volume-backed instance.

        :param instance: nova.objects.instance.Instance object
        :param name: name of the backup or snapshot
        :param extra_properties: dict of extra image properties to include

        :returns: the new image metadata
        """
        image_meta = compute_utils.initialize_instance_snapshot_metadata(context, instance, name, extra_properties)
        image_meta['size'] = 0
        for attr in ('container_format', 'disk_format'):
            image_meta.pop(attr, None)
        properties = image_meta['properties']
        for key in ('block_device_mapping', 'bdm_v2', 'root_device_name'):
            properties.pop(key, None)
        if instance.root_device_name:
            properties['root_device_name'] = instance.root_device_name
        bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(context, instance.uuid)
        mapping = []
        volume_bdms = []
        for bdm in bdms:
            if bdm.no_device:
                continue
            if bdm.is_volume:
                volume_bdms.append(bdm)
            else:
                mapping.append(bdm.get_image_mapping())
        if volume_bdms:
            limits = self.volume_api.get_absolute_limits(context)
            total_snapshots_used = limits['totalSnapshotsUsed']
            max_snapshots = limits['maxTotalSnapshots']
            if max_snapshots > -1 and len(volume_bdms) + total_snapshots_used > max_snapshots:
                LOG.debug('Unable to create volume snapshots for instance. Currently has %s snapshots, requesting %s new snapshots, with a limit of %s.', total_snapshots_used, len(volume_bdms), max_snapshots, instance=instance)
                raise exception.OverQuota(overs='snapshots')
        quiesced = False
        if instance.vm_state == vm_states.ACTIVE:
            try:
                LOG.info('Attempting to quiesce instance before volume snapshot.', instance=instance)
                self.compute_rpcapi.quiesce_instance(context, instance)
                quiesced = True
            except (exception.InstanceQuiesceNotSupported, exception.QemuGuestAgentNotEnabled, exception.NovaException, NotImplementedError) as err:
                if strutils.bool_from_string(instance.system_metadata.get('image_os_require_quiesce')):
                    raise
                if isinstance(err, exception.NovaException):
                    LOG.info('Skipping quiescing instance: %(reason)s.', {'reason': err.format_message()}, instance=instance)
                else:
                    LOG.info('Skipping quiescing instance because the operation is not supported by the underlying compute driver.', instance=instance)
            except Exception as ex:
                with excutils.save_and_reraise_exception():
                    LOG.error('An error occurred during quiesce of instance. Unquiescing to ensure instance is thawed. Error: %s', str(ex), instance=instance)
                    self.compute_rpcapi.unquiesce_instance(context, instance, mapping=None)

        @wrap_instance_event(prefix='api')
        def snapshot_instance(self, context, instance, bdms):
            try:
                for bdm in volume_bdms:
                    volume = self.volume_api.get(context, bdm.volume_id)
                    name = _('snapshot for %s') % image_meta['name']
                    LOG.debug('Creating snapshot from volume %s.', volume['id'], instance=instance)
                    snapshot = self.volume_api.create_snapshot_force(context, volume['id'], name, volume['display_description'])
                    mapping_dict = block_device.snapshot_from_bdm(snapshot['id'], bdm)
                    mapping_dict = mapping_dict.get_image_mapping()
                    mapping.append(mapping_dict)
                return mapping
            except Exception:
                with excutils.save_and_reraise_exception():
                    if quiesced:
                        LOG.info('Unquiescing instance after volume snapshot failure.', instance=instance)
                        self.compute_rpcapi.unquiesce_instance(context, instance, mapping)
        self._record_action_start(context, instance, instance_actions.CREATE_IMAGE)
        mapping = snapshot_instance(self, context, instance, bdms)
        if quiesced:
            self.compute_rpcapi.unquiesce_instance(context, instance, mapping)
        if mapping:
            properties['block_device_mapping'] = mapping
            properties['bdm_v2'] = True
        return self.image_api.create(context, image_meta)

    @check_instance_lock
    def reboot(self, context, instance, reboot_type):
        """Reboot the given instance."""
        if reboot_type == 'SOFT':
            self._soft_reboot(context, instance)
        else:
            self._hard_reboot(context, instance)

    @check_instance_state(vm_state=set(vm_states.ALLOW_SOFT_REBOOT), task_state=[None])
    def _soft_reboot(self, context, instance):
        expected_task_state = [None]
        instance.task_state = task_states.REBOOTING
        instance.save(expected_task_state=expected_task_state)
        self._record_action_start(context, instance, instance_actions.REBOOT)
        self.compute_rpcapi.reboot_instance(context, instance=instance, block_device_info=None, reboot_type='SOFT')

    @check_instance_state(vm_state=set(vm_states.ALLOW_HARD_REBOOT), task_state=task_states.ALLOW_REBOOT)
    def _hard_reboot(self, context, instance):
        instance.task_state = task_states.REBOOTING_HARD
        instance.save(expected_task_state=task_states.ALLOW_REBOOT)
        self._record_action_start(context, instance, instance_actions.REBOOT)
        self.compute_rpcapi.reboot_instance(context, instance=instance, block_device_info=None, reboot_type='HARD')

    def _check_image_arch(self, image=None):
        if image:
            img_arch = image.get('properties', {}).get('hw_architecture')
            if img_arch:
                fields_obj.Architecture.canonicalize(img_arch)

    @reject_vtpm_instances(instance_actions.REBUILD)
    @block_accelerators(until_service=SUPPORT_ACCELERATOR_SERVICE_FOR_REBUILD)
    @check_instance_lock
    @check_instance_state(vm_state=[vm_states.ACTIVE, vm_states.STOPPED, vm_states.ERROR])
    def rebuild(self, context, instance, image_href, admin_password, files_to_inject=None, **kwargs):
        """Rebuild the given instance with the provided attributes."""
        files_to_inject = files_to_inject or []
        metadata = kwargs.get('metadata', {})
        preserve_ephemeral = kwargs.get('preserve_ephemeral', False)
        auto_disk_config = kwargs.get('auto_disk_config')
        if 'key_name' in kwargs:
            key_name = kwargs.pop('key_name')
            if key_name:
                key_pair = objects.KeyPair.get_by_name(context, context.user_id, key_name)
                instance.key_name = key_pair.name
                instance.key_data = key_pair.public_key
                instance.keypairs = objects.KeyPairList(objects=[key_pair])
            else:
                instance.key_name = None
                instance.key_data = None
                instance.keypairs = objects.KeyPairList(objects=[])
        trusted_certs = None
        if 'trusted_certs' in kwargs:
            trusted_certs = kwargs.pop('trusted_certs')
            instance.trusted_certs = self._retrieve_trusted_certs_object(context, trusted_certs, rebuild=True)
        (image_id, image) = self._get_image(context, image_href)
        self._check_auto_disk_config(image=image, auto_disk_config=auto_disk_config)
        self._check_image_arch(image=image)
        flavor = instance.get_flavor()
        bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(context, instance.uuid)
        root_bdm = compute_utils.get_root_bdm(context, instance, bdms)
        is_volume_backed = compute_utils.is_volume_backed_instance(context, instance, bdms)
        if is_volume_backed:
            if trusted_certs:
                msg = _('Image certificate validation is not supported for volume-backed servers.')
                raise exception.CertificateValidationFailed(message=msg)
            if root_bdm is None:
                raise exception.NovaException(_('Unable to find root block device mapping for volume-backed instance.'))
            volume = self.volume_api.get(context, root_bdm.volume_id)
            volume_image_metadata = volume.get('volume_image_metadata', {})
            orig_image_ref = volume_image_metadata.get('image_id')
            if orig_image_ref != image_href:
                LOG.debug('Requested to rebuild instance with a new image %s for a volume-backed server with image %s in its root volume which is not supported.', image_href, orig_image_ref, instance=instance)
                msg = _('Unable to rebuild with a different image for a volume-backed server.')
                raise exception.ImageUnacceptable(image_id=image_href, reason=msg)
        else:
            orig_image_ref = instance.image_ref
        request_spec = objects.RequestSpec.get_by_instance_uuid(context, instance.uuid)
        self._checks_for_create_and_rebuild(context, image_id, image, flavor, metadata, files_to_inject, root_bdm)
        self._check_volume_status(context, bdms)
        if orig_image_ref != image_href:
            self._validate_numa_rebuild(instance, image, flavor)
        (kernel_id, ramdisk_id) = self._handle_kernel_and_ramdisk(context, None, None, image)

        def _reset_image_metadata():
            """Remove old image properties that we're storing as instance
            system metadata.  These properties start with 'image_'.
            Then add the properties for the new image.
            """
            orig_sys_metadata = dict(instance.system_metadata)
            for key in list(instance.system_metadata.keys()):
                if key.startswith(utils.SM_IMAGE_PROP_PREFIX):
                    del instance.system_metadata[key]
            new_sys_metadata = utils.get_system_metadata_from_image(image, flavor)
            new_sys_metadata.update({'image_base_image_ref': image_id})
            instance.system_metadata.update(new_sys_metadata)
            instance.save()
            return orig_sys_metadata
        options_from_image = self._inherit_properties_from_image(image, auto_disk_config)
        instance.update(options_from_image)
        instance.task_state = task_states.REBUILDING
        if not is_volume_backed:
            instance.image_ref = image_href
        instance.kernel_id = kernel_id or ''
        instance.ramdisk_id = ramdisk_id or ''
        instance.progress = 0
        instance.update(kwargs)
        instance.save(expected_task_state=[None])
        orig_sys_metadata = _reset_image_metadata()
        self._record_action_start(context, instance, instance_actions.REBUILD)
        host = instance.host
        if orig_image_ref != image_href:
            request_spec.image = objects.ImageMeta.from_dict(image)
            request_spec.save()
            if 'scheduler_hints' not in request_spec:
                request_spec.scheduler_hints = {}
            del request_spec.id
            request_spec.scheduler_hints['_nova_check_type'] = ['rebuild']
            request_spec.force_hosts = [instance.host]
            request_spec.force_nodes = [instance.node]
            host = None
        self.compute_task_api.rebuild_instance(context, instance=instance, new_pass=admin_password, injected_files=files_to_inject, image_ref=image_href, orig_image_ref=orig_image_ref, orig_sys_metadata=orig_sys_metadata, bdms=bdms, preserve_ephemeral=preserve_ephemeral, host=host, request_spec=request_spec)

    def _check_volume_status(self, context, bdms):
        """Check whether the status of the volume is "in-use".

        :param context: A context.RequestContext
        :param bdms: BlockDeviceMappingList of BDMs for the instance
        """
        for bdm in bdms:
            if bdm.volume_id:
                vol = self.volume_api.get(context, bdm.volume_id)
                self.volume_api.check_attached(context, vol)

    @staticmethod
    def _validate_numa_rebuild(instance, image, flavor):
        """validates that the NUMA constraints do not change on rebuild.

        :param instance: nova.objects.instance.Instance object
        :param image: the new image the instance will be rebuilt with.
        :param flavor: the flavor of the current instance.
        :raises: nova.exception.ImageNUMATopologyRebuildConflict
        """
        old_image_meta = instance.image_meta
        new_image_meta = objects.ImageMeta.from_dict(image)
        old_constraints = hardware.numa_get_constraints(flavor, old_image_meta)
        new_constraints = hardware.numa_get_constraints(flavor, new_image_meta)
        if old_constraints is None and new_constraints is None:
            return
        if old_constraints is None or new_constraints is None:
            action = 'removing' if old_constraints else 'introducing'
            LOG.debug('NUMA rebuild validation failed. The requested image would alter the NUMA constraints by %s a NUMA topology.', action, instance=instance)
            raise exception.ImageNUMATopologyRebuildConflict()
        old = old_constraints.obj_to_primitive()
        new = new_constraints.obj_to_primitive()
        if old != new:
            LOG.debug('NUMA rebuild validation failed. The requested image conflicts with the existing NUMA constraints.', instance=instance)
            raise exception.ImageNUMATopologyRebuildConflict()

    @staticmethod
    def _check_quota_for_upsize(context, instance, current_flavor, new_flavor):
        (project_id, user_id) = quotas_obj.ids_from_instance(context, instance)
        deltas = compute_utils.upsize_quota_delta(new_flavor, current_flavor)
        if deltas:
            try:
                res_deltas = {'cores': deltas.get('cores', 0), 'ram': deltas.get('ram', 0)}
                objects.Quotas.check_deltas(context, res_deltas, project_id, user_id=user_id, check_project_id=project_id, check_user_id=user_id)
            except exception.OverQuota as exc:
                quotas = exc.kwargs['quotas']
                overs = exc.kwargs['overs']
                usages = exc.kwargs['usages']
                headroom = compute_utils.get_headroom(quotas, usages, deltas)
                (overs, reqs, total_alloweds, useds) = compute_utils.get_over_quota_detail(headroom, overs, quotas, deltas)
                LOG.info('%(overs)s quota exceeded for %(pid)s, tried to resize instance.', {'overs': overs, 'pid': context.project_id})
                raise exception.TooManyInstances(overs=overs, req=reqs, used=useds, allowed=total_alloweds)

    @check_instance_lock
    @check_instance_state(vm_state=[vm_states.RESIZED])
    def revert_resize(self, context, instance):
        """Reverts a resize or cold migration, deleting the 'new' instance in
        the process.
        """
        elevated = context.elevated()
        migration = objects.Migration.get_by_instance_and_status(elevated, instance.uuid, 'finished')
        self._check_quota_for_upsize(context, instance, instance.flavor, instance.old_flavor)
        instance.availability_zone = availability_zones.get_host_availability_zone(context, migration.source_compute)
        reqspec = objects.RequestSpec.get_by_instance_uuid(context, instance.uuid)
        if reqspec.flavor['id'] != instance.old_flavor['id']:
            reqspec.flavor = instance.old_flavor
            reqspec.numa_topology = hardware.numa_get_constraints(instance.old_flavor, instance.image_meta)
            reqspec.save()
        if instance.get_network_info().has_port_with_allocation():
            port_res_req = self.network_api.get_requested_resource_for_instance(context, instance.uuid)
            reqspec.requested_resources = port_res_req
        instance.task_state = task_states.RESIZE_REVERTING
        instance.save(expected_task_state=[None])
        migration.status = 'reverting'
        migration.save()
        self._record_action_start(context, instance, instance_actions.REVERT_RESIZE)
        if migration.cross_cell_move:
            self.compute_task_api.revert_snapshot_based_resize(context, instance, migration)
        else:
            self.compute_rpcapi.revert_resize(context, instance, migration, migration.dest_compute, reqspec)

    @staticmethod
    def _get_source_compute_service(context, migration):
        """Find the source compute Service object given the Migration.

        :param context: nova auth RequestContext target at the destination
            compute cell
        :param migration: Migration object for the move operation
        :return: Service object representing the source host nova-compute
        """
        if migration.cross_cell_move:
            hm = objects.HostMapping.get_by_host(context, migration.source_compute)
            with nova_context.target_cell(context, hm.cell_mapping) as cctxt:
                return objects.Service.get_by_compute_host(cctxt, migration.source_compute)
        return objects.Service.get_by_compute_host(context, migration.source_compute)

    @check_instance_lock
    @check_instance_state(vm_state=[vm_states.RESIZED])
    def confirm_resize(self, context, instance, migration=None):
        """Confirms a migration/resize and deletes the 'old' instance.

        :param context: nova auth RequestContext
        :param instance: Instance object to confirm the resize
        :param migration: Migration object; provided if called from the
            _poll_unconfirmed_resizes periodic task on the dest compute.
        :raises: MigrationNotFound if migration is not provided and a migration
            cannot be found for the instance with status "finished".
        :raises: ServiceUnavailable if the source compute service is down.
        """
        elevated = context.elevated()
        if migration is None:
            migration = objects.Migration.get_by_instance_and_status(elevated, instance.uuid, 'finished')
        source_svc = self._get_source_compute_service(context, migration)
        if not self.servicegroup_api.service_is_up(source_svc):
            raise exception.ServiceUnavailable()
        migration.status = 'confirming'
        migration.save()
        self._record_action_start(context, instance, instance_actions.CONFIRM_RESIZE)
        if migration.cross_cell_move:
            self.compute_task_api.confirm_snapshot_based_resize(context, instance, migration)
        else:
            self.compute_rpcapi.confirm_resize(context, instance, migration, migration.source_compute)

    def _allow_cross_cell_resize(self, context, instance):
        """Determine if the request can perform a cross-cell resize on this
        instance.

        :param context: nova auth request context for the resize operation
        :param instance: Instance object being resized
        :returns: True if cross-cell resize is allowed, False otherwise
        """
        allowed = context.can(servers_policies.CROSS_CELL_RESIZE, target={'user_id': instance.user_id, 'project_id': instance.project_id}, fatal=False)
        if allowed:
            min_compute_version = objects.service.get_minimum_version_all_cells(context, ['nova-compute'])
            if min_compute_version < MIN_COMPUTE_CROSS_CELL_RESIZE:
                LOG.debug('Request is allowed by policy to perform cross-cell resize but the minimum nova-compute service version in the deployment %s is less than %s so cross-cell resize is not allowed at this time.', min_compute_version, MIN_COMPUTE_CROSS_CELL_RESIZE)
                return False
            if self.network_api.get_requested_resource_for_instance(context, instance.uuid):
                LOG.info('Request is allowed by policy to perform cross-cell resize but the instance has ports with resource request and cross-cell resize is not supported with such ports.', instance=instance)
                return False
        return allowed

    @staticmethod
    def _validate_host_for_cold_migrate(context, instance, host_name, allow_cross_cell_resize):
        """Validates a host specified for cold migration.

        :param context: nova auth request context for the cold migration
        :param instance: Instance object being cold migrated
        :param host_name: User-specified compute service hostname for the
            desired destination of the instance during the cold migration
        :param allow_cross_cell_resize: If True, cross-cell resize is allowed
            for this operation and the host could be in a different cell from
            the one that the instance is currently in. If False, the speciifed
            host must be in the same cell as the instance.
        :returns: ComputeNode object of the requested host
        :raises: CannotMigrateToSameHost if the host is the same as the
            current instance.host
        :raises: ComputeHostNotFound if the specified host cannot be found
        """
        if host_name == instance.host:
            raise exception.CannotMigrateToSameHost()
        if allow_cross_cell_resize:
            try:
                hm = objects.HostMapping.get_by_host(context, host_name)
            except exception.HostMappingNotFound:
                LOG.info('HostMapping not found for host: %s', host_name)
                raise exception.ComputeHostNotFound(host=host_name)
            with nova_context.target_cell(context, hm.cell_mapping) as cctxt:
                node = objects.ComputeNode.get_first_node_by_host_for_old_compat(cctxt, host_name, use_slave=True)
        else:
            node = objects.ComputeNode.get_first_node_by_host_for_old_compat(context, host_name, use_slave=True)
        return node

    @reject_vdpa_instances(instance_actions.RESIZE)
    @block_accelerators()
    @check_instance_lock
    @check_instance_state(vm_state=[vm_states.ACTIVE, vm_states.STOPPED])
    @check_instance_host(check_is_up=True)
    def resize(self, context, instance, flavor_id=None, clean_shutdown=True, host_name=None, auto_disk_config=None):
        """Resize (ie, migrate) a running instance.

        If flavor_id is None, the process is considered a migration, keeping
        the original flavor_id. If flavor_id is not None, the instance should
        be migrated to a new host and resized to the new flavor_id.
        host_name is always None in the resize case.
        host_name can be set in the cold migration case only.
        """
        allow_cross_cell_resize = self._allow_cross_cell_resize(context, instance)
        if host_name is not None:
            node = self._validate_host_for_cold_migrate(context, instance, host_name, allow_cross_cell_resize)
        self._check_auto_disk_config(instance, auto_disk_config=auto_disk_config)
        current_instance_type = instance.get_flavor()
        instance.system_metadata.update({'image_base_image_ref': instance.image_ref})
        volume_backed = None
        if not flavor_id:
            LOG.debug('flavor_id is None. Assuming migration.', instance=instance)
            new_instance_type = current_instance_type
        else:
            new_instance_type = flavors.get_flavor_by_flavor_id(flavor_id, read_deleted='no')
            if new_instance_type.extra_specs.get('accel:device_profile'):
                raise exception.ForbiddenWithAccelerators()
            if new_instance_type.get('root_gb') == 0 and current_instance_type.get('root_gb') != 0:
                volume_backed = compute_utils.is_volume_backed_instance(context, instance)
                if not volume_backed:
                    reason = _('Resize to zero disk flavor is not allowed.')
                    raise exception.CannotResizeDisk(reason=reason)
        current_instance_type_name = current_instance_type['name']
        new_instance_type_name = new_instance_type['name']
        LOG.debug('Old instance type %(current_instance_type_name)s, new instance type %(new_instance_type_name)s', {'current_instance_type_name': current_instance_type_name, 'new_instance_type_name': new_instance_type_name}, instance=instance)
        same_instance_type = current_instance_type['id'] == new_instance_type['id']
        if not same_instance_type and new_instance_type.get('disabled'):
            raise exception.FlavorNotFound(flavor_id=flavor_id)
        if same_instance_type and flavor_id:
            raise exception.CannotResizeToSameFlavor()
        if flavor_id:
            self._check_quota_for_upsize(context, instance, current_instance_type, new_instance_type)
        if not same_instance_type:
            image = utils.get_image_from_system_metadata(instance.system_metadata)
            if volume_backed is None:
                volume_backed = compute_utils.is_volume_backed_instance(context, instance)
            if volume_backed:
                self._validate_flavor_image_numa_pci(image, new_instance_type, validate_pci=True)
            else:
                self._validate_flavor_image_nostatus(context, image, new_instance_type, root_bdm=None, validate_pci=True)
        filter_properties = {'ignore_hosts': []}
        if not self._allow_resize_to_same_host(same_instance_type, instance):
            filter_properties['ignore_hosts'].append(instance.host)
        request_spec = objects.RequestSpec.get_by_instance_uuid(context, instance.uuid)
        request_spec.ignore_hosts = filter_properties['ignore_hosts']
        if not same_instance_type:
            request_spec.numa_topology = hardware.numa_get_constraints(new_instance_type, instance.image_meta)
            self._check_compute_service_for_mixed_instance(request_spec.numa_topology)
        instance.task_state = task_states.RESIZE_PREP
        instance.progress = 0
        instance.auto_disk_config = auto_disk_config or False
        instance.save(expected_task_state=[None])
        if not flavor_id:
            self._record_action_start(context, instance, instance_actions.MIGRATE)
        else:
            self._record_action_start(context, instance, instance_actions.RESIZE)
        scheduler_hint = {'filter_properties': filter_properties}
        if host_name is None:
            request_spec.requested_destination = objects.Destination(allow_cross_cell_move=allow_cross_cell_resize)
        else:
            request_spec.requested_destination = objects.Destination(host=node.host, node=node.hypervisor_hostname, allow_cross_cell_move=allow_cross_cell_resize)
        self.compute_task_api.resize_instance(context, instance, scheduler_hint=scheduler_hint, flavor=new_instance_type, clean_shutdown=clean_shutdown, request_spec=request_spec, do_cast=True)

    def _allow_resize_to_same_host(self, cold_migrate, instance):
        """Contains logic for excluding the instance.host on resize/migrate.

        If performing a cold migration and the compute node resource provider
        reports the COMPUTE_SAME_HOST_COLD_MIGRATE trait then same-host cold
        migration is allowed otherwise it is not and the current instance.host
        should be excluded as a scheduling candidate.

        :param cold_migrate: true if performing a cold migration, false
            for resize
        :param instance: Instance object being resized or cold migrated
        :returns: True if same-host resize/cold migrate is allowed, False
            otherwise
        """
        if cold_migrate:
            ctxt = instance._context
            cn = objects.ComputeNode.get_by_host_and_nodename(ctxt, instance.host, instance.node)
            traits = self.placementclient.get_provider_traits(ctxt, cn.uuid).traits
            if os_traits.COMPUTE_SAME_HOST_COLD_MIGRATE in traits:
                allow_same_host = True
            else:
                service = objects.Service.get_by_compute_host(ctxt, cn.host)
                if service.version >= MIN_COMPUTE_SAME_HOST_COLD_MIGRATE:
                    allow_same_host = False
                else:
                    allow_same_host = CONF.allow_resize_to_same_host
        else:
            allow_same_host = CONF.allow_resize_to_same_host
        return allow_same_host

    @reject_vdpa_instances(instance_actions.SHELVE)
    @reject_vtpm_instances(instance_actions.SHELVE)
    @block_accelerators(until_service=54)
    @check_instance_lock
    @check_instance_state(vm_state=[vm_states.ACTIVE, vm_states.STOPPED, vm_states.PAUSED, vm_states.SUSPENDED])
    def shelve(self, context, instance, clean_shutdown=True):
        """Shelve an instance.

        Shuts down an instance and frees it up to be removed from the
        hypervisor.
        """
        instance.task_state = task_states.SHELVING
        instance.system_metadata.update({'image_base_image_ref': instance.image_ref})
        instance.save(expected_task_state=[None])
        self._record_action_start(context, instance, instance_actions.SHELVE)
        accel_uuids = []
        if instance.flavor.extra_specs.get('accel:device_profile'):
            cyclient = cyborg.get_client(context)
            accel_uuids = cyclient.get_arq_uuids_for_instance(instance)
        if not compute_utils.is_volume_backed_instance(context, instance):
            name = '%s-shelved' % instance.display_name
            image_meta = compute_utils.create_image(context, instance, name, 'snapshot', self.image_api)
            image_id = image_meta['id']
            self.compute_rpcapi.shelve_instance(context, instance=instance, image_id=image_id, clean_shutdown=clean_shutdown, accel_uuids=accel_uuids)
        else:
            self.compute_rpcapi.shelve_offload_instance(context, instance=instance, clean_shutdown=clean_shutdown, accel_uuids=accel_uuids)

    @check_instance_lock
    @check_instance_state(vm_state=[vm_states.SHELVED])
    def shelve_offload(self, context, instance, clean_shutdown=True):
        """Remove a shelved instance from the hypervisor."""
        instance.task_state = task_states.SHELVING_OFFLOADING
        instance.save(expected_task_state=[None])
        self._record_action_start(context, instance, instance_actions.SHELVE_OFFLOAD)
        accel_uuids = []
        if instance.flavor.extra_specs.get('accel:device_profile'):
            cyclient = cyborg.get_client(context)
            accel_uuids = cyclient.get_arq_uuids_for_instance(instance)
        self.compute_rpcapi.shelve_offload_instance(context, instance=instance, clean_shutdown=clean_shutdown, accel_uuids=accel_uuids)

    def _validate_unshelve_az(self, context, instance, availability_zone):
        """Verify the specified availability_zone during unshelve.

        Verifies that the server is shelved offloaded, the AZ exists and
        if [cinder]/cross_az_attach=False, that any attached volumes are in
        the same AZ.

        :param context: nova auth RequestContext for the unshelve action
        :param instance: Instance object for the server being unshelved
        :param availability_zone: The user-requested availability zone in
            which to unshelve the server.
        :raises: UnshelveInstanceInvalidState if the server is not shelved
            offloaded
        :raises: InvalidRequest if the requested AZ does not exist
        :raises: MismatchVolumeAZException if [cinder]/cross_az_attach=False
            and any attached volumes are not in the requested AZ
        """
        if instance.vm_state != vm_states.SHELVED_OFFLOADED:
            raise exception.UnshelveInstanceInvalidState(state=instance.vm_state, instance_uuid=instance.uuid)
        available_zones = availability_zones.get_availability_zones(context, self.host_api, get_only_available=True)
        if availability_zone not in available_zones:
            msg = _('The requested availability zone is not available')
            raise exception.InvalidRequest(msg)
        if not CONF.cinder.cross_az_attach:
            bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(context, instance.uuid)
            for bdm in bdms:
                if bdm.is_volume and bdm.volume_id:
                    volume = self.volume_api.get(context, bdm.volume_id)
                    if availability_zone != volume['availability_zone']:
                        msg = _('The specified availability zone does not match the volume %(vol_id)s attached to the server. Specified availability zone is %(az)s. Volume is in %(vol_zone)s.') % {'vol_id': volume['id'], 'az': availability_zone, 'vol_zone': volume['availability_zone']}
                        raise exception.MismatchVolumeAZException(reason=msg)

    @check_instance_lock
    @check_instance_state(vm_state=[vm_states.SHELVED, vm_states.SHELVED_OFFLOADED])
    def unshelve(self, context, instance, new_az=None):
        """Restore a shelved instance."""
        request_spec = objects.RequestSpec.get_by_instance_uuid(context, instance.uuid)
        if new_az:
            self._validate_unshelve_az(context, instance, new_az)
            LOG.debug('Replace the old AZ %(old_az)s in RequestSpec with a new AZ %(new_az)s of the instance.', {'old_az': request_spec.availability_zone, 'new_az': new_az}, instance=instance)
            request_spec.availability_zone = new_az
            request_spec.save()
        instance.task_state = task_states.UNSHELVING
        instance.save(expected_task_state=[None])
        self._record_action_start(context, instance, instance_actions.UNSHELVE)
        self.compute_task_api.unshelve_instance(context, instance, request_spec)

    @check_instance_lock
    def add_fixed_ip(self, context, instance, network_id):
        """Add fixed_ip from specified network to given instance."""
        self.compute_rpcapi.add_fixed_ip_to_instance(context, instance=instance, network_id=network_id)

    @check_instance_lock
    def remove_fixed_ip(self, context, instance, address):
        """Remove fixed_ip from specified network to given instance."""
        self.compute_rpcapi.remove_fixed_ip_from_instance(context, instance=instance, address=address)

    @check_instance_lock
    @check_instance_state(vm_state=[vm_states.ACTIVE])
    def pause(self, context, instance):
        """Pause the given instance."""
        instance.task_state = task_states.PAUSING
        instance.save(expected_task_state=[None])
        self._record_action_start(context, instance, instance_actions.PAUSE)
        self.compute_rpcapi.pause_instance(context, instance)

    @check_instance_lock
    @check_instance_state(vm_state=[vm_states.PAUSED])
    def unpause(self, context, instance):
        """Unpause the given instance."""
        instance.task_state = task_states.UNPAUSING
        instance.save(expected_task_state=[None])
        self._record_action_start(context, instance, instance_actions.UNPAUSE)
        self.compute_rpcapi.unpause_instance(context, instance)

    @check_instance_host()
    def get_diagnostics(self, context, instance):
        """Retrieve diagnostics for the given instance."""
        return self.compute_rpcapi.get_diagnostics(context, instance=instance)

    @check_instance_host()
    def get_instance_diagnostics(self, context, instance):
        """Retrieve diagnostics for the given instance."""
        return self.compute_rpcapi.get_instance_diagnostics(context, instance=instance)

    @reject_vdpa_instances(instance_actions.SUSPEND)
    @block_accelerators()
    @reject_sev_instances(instance_actions.SUSPEND)
    @check_instance_lock
    @check_instance_state(vm_state=[vm_states.ACTIVE])
    def suspend(self, context, instance):
        """Suspend the given instance."""
        instance.task_state = task_states.SUSPENDING
        instance.save(expected_task_state=[None])
        self._record_action_start(context, instance, instance_actions.SUSPEND)
        self.compute_rpcapi.suspend_instance(context, instance)

    @check_instance_lock
    @check_instance_state(vm_state=[vm_states.SUSPENDED])
    def resume(self, context, instance):
        """Resume the given instance."""
        instance.task_state = task_states.RESUMING
        instance.save(expected_task_state=[None])
        self._record_action_start(context, instance, instance_actions.RESUME)
        self.compute_rpcapi.resume_instance(context, instance)

    @reject_vtpm_instances(instance_actions.RESCUE)
    @check_instance_lock
    @check_instance_state(vm_state=[vm_states.ACTIVE, vm_states.STOPPED, vm_states.ERROR])
    def rescue(self, context, instance, rescue_password=None, rescue_image_ref=None, clean_shutdown=True, allow_bfv_rescue=False):
        """Rescue the given instance."""
        if rescue_image_ref:
            try:
                image_meta = image_meta_obj.ImageMeta.from_image_ref(context, self.image_api, rescue_image_ref)
            except (exception.ImageNotFound, exception.ImageBadRequest):
                LOG.warning('Failed to fetch rescue image metadata using image_ref %(image_ref)s', {'image_ref': rescue_image_ref})
                raise exception.UnsupportedRescueImage(image=rescue_image_ref)
            if image_meta.properties.get('img_block_device_mapping'):
                LOG.warning('Unable to rescue an instance using a volume snapshot image with img_block_device_mapping image properties set')
                raise exception.UnsupportedRescueImage(image=rescue_image_ref)
        bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(context, instance.uuid)
        self._check_volume_status(context, bdms)
        volume_backed = compute_utils.is_volume_backed_instance(context, instance, bdms)
        if volume_backed and allow_bfv_rescue:
            cn = objects.ComputeNode.get_by_host_and_nodename(context, instance.host, instance.node)
            traits = self.placementclient.get_provider_traits(context, cn.uuid).traits
            if os_traits.COMPUTE_RESCUE_BFV not in traits:
                reason = _('Host unable to rescue a volume-backed instance')
                raise exception.InstanceNotRescuable(instance_id=instance.uuid, reason=reason)
        elif volume_backed:
            reason = _('Cannot rescue a volume-backed instance')
            raise exception.InstanceNotRescuable(instance_id=instance.uuid, reason=reason)
        instance.task_state = task_states.RESCUING
        instance.save(expected_task_state=[None])
        self._record_action_start(context, instance, instance_actions.RESCUE)
        self.compute_rpcapi.rescue_instance(context, instance=instance, rescue_password=rescue_password, rescue_image_ref=rescue_image_ref, clean_shutdown=clean_shutdown)

    @check_instance_lock
    @check_instance_state(vm_state=[vm_states.RESCUED])
    def unrescue(self, context, instance):
        """Unrescue the given instance."""
        instance.task_state = task_states.UNRESCUING
        instance.save(expected_task_state=[None])
        self._record_action_start(context, instance, instance_actions.UNRESCUE)
        self.compute_rpcapi.unrescue_instance(context, instance=instance)

    @check_instance_lock
    @check_instance_state(vm_state=[vm_states.ACTIVE])
    def set_admin_password(self, context, instance, password):
        """Set the root/admin password for the given instance.

        @param context: Nova auth context.
        @param instance: Nova instance object.
        @param password: The admin password for the instance.
        """
        instance.task_state = task_states.UPDATING_PASSWORD
        instance.save(expected_task_state=[None])
        self._record_action_start(context, instance, instance_actions.CHANGE_PASSWORD)
        self.compute_rpcapi.set_admin_password(context, instance=instance, new_pass=password)

    @check_instance_host()
    @reject_instance_state(task_state=[task_states.DELETING, task_states.MIGRATING])
    def get_vnc_console(self, context, instance, console_type):
        """Get a url to an instance Console."""
        connect_info = self.compute_rpcapi.get_vnc_console(context, instance=instance, console_type=console_type)
        return {'url': connect_info['access_url']}

    @check_instance_host()
    @reject_instance_state(task_state=[task_states.DELETING, task_states.MIGRATING])
    def get_spice_console(self, context, instance, console_type):
        """Get a url to an instance Console."""
        connect_info = self.compute_rpcapi.get_spice_console(context, instance=instance, console_type=console_type)
        return {'url': connect_info['access_url']}

    @check_instance_host()
    @reject_instance_state(task_state=[task_states.DELETING, task_states.MIGRATING])
    def get_rdp_console(self, context, instance, console_type):
        """Get a url to an instance Console."""
        connect_info = self.compute_rpcapi.get_rdp_console(context, instance=instance, console_type=console_type)
        return {'url': connect_info['access_url']}

    @check_instance_host()
    @reject_instance_state(task_state=[task_states.DELETING, task_states.MIGRATING])
    def get_serial_console(self, context, instance, console_type):
        """Get a url to a serial console."""
        connect_info = self.compute_rpcapi.get_serial_console(context, instance=instance, console_type=console_type)
        return {'url': connect_info['access_url']}

    @check_instance_host()
    @reject_instance_state(task_state=[task_states.DELETING, task_states.MIGRATING])
    def get_mks_console(self, context, instance, console_type):
        """Get a url to a MKS console."""
        connect_info = self.compute_rpcapi.get_mks_console(context, instance=instance, console_type=console_type)
        return {'url': connect_info['access_url']}

    @check_instance_host()
    def get_console_output(self, context, instance, tail_length=None):
        """Get console output for an instance."""
        return self.compute_rpcapi.get_console_output(context, instance=instance, tail_length=tail_length)

    def lock(self, context, instance, reason=None):
        """Lock the given instance."""
        is_owner = instance.project_id == context.project_id
        if instance.locked and is_owner:
            return
        context = context.elevated()
        self._record_action_start(context, instance, instance_actions.LOCK)

        @wrap_instance_event(prefix='api')
        def lock(self, context, instance, reason=None):
            LOG.debug('Locking', instance=instance)
            instance.locked = True
            instance.locked_by = 'owner' if is_owner else 'admin'
            if reason:
                instance.system_metadata['locked_reason'] = reason
            instance.save()
        lock(self, context, instance, reason=reason)
        compute_utils.notify_about_instance_action(context, instance, CONF.host, action=fields_obj.NotificationAction.LOCK, source=fields_obj.NotificationSource.API)

    def is_expected_locked_by(self, context, instance):
        is_owner = instance.project_id == context.project_id
        expect_locked_by = 'owner' if is_owner else 'admin'
        locked_by = instance.locked_by
        if locked_by and locked_by != expect_locked_by:
            return False
        return True

    def unlock(self, context, instance):
        """Unlock the given instance."""
        context = context.elevated()
        self._record_action_start(context, instance, instance_actions.UNLOCK)

        @wrap_instance_event(prefix='api')
        def unlock(self, context, instance):
            LOG.debug('Unlocking', instance=instance)
            instance.locked = False
            instance.locked_by = None
            instance.system_metadata.pop('locked_reason', None)
            instance.save()
        unlock(self, context, instance)
        compute_utils.notify_about_instance_action(context, instance, CONF.host, action=fields_obj.NotificationAction.UNLOCK, source=fields_obj.NotificationSource.API)

    @check_instance_lock
    def inject_network_info(self, context, instance):
        """Inject network info for the instance."""
        self.compute_rpcapi.inject_network_info(context, instance=instance)

    def _create_volume_bdm(self, context, instance, device, volume, disk_bus, device_type, is_local_creation=False, tag=None, delete_on_termination=False):
        volume_id = volume['id']
        if is_local_creation:
            volume_bdm = objects.BlockDeviceMapping(context=context, source_type='volume', destination_type='volume', instance_uuid=instance.uuid, boot_index=None, volume_id=volume_id, device_name=None, guest_format=None, disk_bus=disk_bus, device_type=device_type, delete_on_termination=delete_on_termination)
            volume_bdm.create()
        else:
            volume_bdm = self.compute_rpcapi.reserve_block_device_name(context, instance, device, volume_id, disk_bus=disk_bus, device_type=device_type, tag=tag, multiattach=volume['multiattach'])
            volume_bdm.delete_on_termination = delete_on_termination
            volume_bdm.save()
        return volume_bdm

    def _check_volume_already_attached(self, context: nova_context.RequestContext, instance: objects.Instance, volume: ty.Mapping[str, ty.Any]):
        """Avoid duplicate volume attachments.

        Since the 3.44 Cinder microversion, Cinder allows us to attach the same
        volume to the same instance twice. This is ostensibly to enable live
        migration, but it's not something we want to occur outside of this
        particular code path.

        In addition, we also need to ensure that non-multiattached volumes are
        not attached to multiple instances. This check is also carried out
        later by c-api itself but it can however be circumvented by admins
        resetting the state of an attached volume to available. As a result we
        also need to perform a check within Nova before creating a new BDM for
        the attachment.

        :param context: nova auth RequestContext
        :param instance: Instance object
        :param volume: volume dict from cinder
        """
        try:
            bdms = objects.BlockDeviceMappingList.get_by_volume(context, volume['id'])
        except exception.VolumeBDMNotFound:
            return
        if not volume.get('multiattach'):
            instance_uuids = ' '.join((f'{b.instance_uuid}' for b in bdms))
            msg = _('volume %(volume_id)s is already attached to instances: %(instance_uuids)s') % {'volume_id': volume['id'], 'instance_uuids': instance_uuids}
            raise exception.InvalidVolume(reason=msg)
        if any((b for b in bdms if b.instance_uuid == instance.uuid)):
            msg = _('volume %s already attached') % volume['id']
            raise exception.InvalidVolume(reason=msg)

    def _check_attach_and_reserve_volume(self, context, volume, instance, bdm, supports_multiattach=False, validate_az=True):
        """Perform checks against the instance and volume before attaching.

        If validation succeeds, the bdm is updated with an attachment_id which
        effectively reserves it during the attach process in cinder.

        :param context: nova auth RequestContext
        :param volume: volume dict from cinder
        :param instance: Instance object
        :param bdm: BlockDeviceMapping object
        :param supports_multiattach: True if the request supports multiattach
            volumes, i.e. microversion >= 2.60, False otherwise
        :param validate_az: True if the instance and volume availability zones
            should be validated for cross_az_attach, False to not validate AZ
        """
        volume_id = volume['id']
        if validate_az:
            self.volume_api.check_availability_zone(context, volume, instance=instance)
        if volume['multiattach'] and (not supports_multiattach):
            raise exception.MultiattachNotSupportedOldMicroversion()
        attachment_id = self.volume_api.attachment_create(context, volume_id, instance.uuid)['id']
        bdm.attachment_id = attachment_id
        if bdm.obj_attr_is_set('id'):
            bdm.save()

    def _attach_volume(self, context, instance, volume, device, disk_bus, device_type, tag=None, supports_multiattach=False, delete_on_termination=False):
        """Attach an existing volume to an existing instance.

        This method is separated to make it possible for cells version
        to override it.
        """
        volume_bdm = self._create_volume_bdm(context, instance, device, volume, disk_bus=disk_bus, device_type=device_type, tag=tag, delete_on_termination=delete_on_termination)
        try:
            self._check_attach_and_reserve_volume(context, volume, instance, volume_bdm, supports_multiattach)
            self._record_action_start(context, instance, instance_actions.ATTACH_VOLUME)
            self.compute_rpcapi.attach_volume(context, instance, volume_bdm)
        except Exception:
            with excutils.save_and_reraise_exception():
                volume_bdm.destroy()
        return volume_bdm.device_name

    def _attach_volume_shelved_offloaded(self, context, instance, volume, device, disk_bus, device_type, delete_on_termination):
        """Attach an existing volume to an instance in shelved offloaded state.

        Attaching a volume for an instance in shelved offloaded state requires
        to perform the regular check to see if we can attach and reserve the
        volume then we need to call the attach method on the volume API
        to mark the volume as 'in-use'.
        The instance at this stage is not managed by a compute manager
        therefore the actual attachment will be performed once the
        instance will be unshelved.
        """
        volume_id = volume['id']

        @wrap_instance_event(prefix='api')
        def attach_volume(self, context, v_id, instance, dev, attachment_id):
            if attachment_id:
                self.volume_api.attachment_complete(context, attachment_id)
            else:
                self.volume_api.attach(context, v_id, instance.uuid, dev)
        volume_bdm = self._create_volume_bdm(context, instance, device, volume, disk_bus=disk_bus, device_type=device_type, is_local_creation=True, delete_on_termination=delete_on_termination)
        try:
            self._check_attach_and_reserve_volume(context, volume, instance, volume_bdm)
            self._record_action_start(context, instance, instance_actions.ATTACH_VOLUME)
            attach_volume(self, context, volume_id, instance, device, volume_bdm.attachment_id)
        except Exception:
            with excutils.save_and_reraise_exception():
                volume_bdm.destroy()
        return volume_bdm.device_name

    @check_instance_lock
    @check_instance_state(vm_state=[vm_states.ACTIVE, vm_states.PAUSED, vm_states.STOPPED, vm_states.RESIZED, vm_states.SOFT_DELETED, vm_states.SHELVED, vm_states.SHELVED_OFFLOADED])
    def attach_volume(self, context, instance, volume_id, device=None, disk_bus=None, device_type=None, tag=None, supports_multiattach=False, delete_on_termination=False):
        """Attach an existing volume to an existing instance."""
        if device and (not block_device.match_device(device)):
            raise exception.InvalidDevicePath(path=device)
        volume = self.volume_api.get(context, volume_id)
        self._check_volume_already_attached(context, instance, volume)
        is_shelved_offloaded = instance.vm_state == vm_states.SHELVED_OFFLOADED
        if is_shelved_offloaded:
            if tag:
                raise exception.VolumeTaggedAttachToShelvedNotSupported()
            if volume['multiattach']:
                raise exception.MultiattachToShelvedNotSupported()
            return self._attach_volume_shelved_offloaded(context, instance, volume, device, disk_bus, device_type, delete_on_termination)
        return self._attach_volume(context, instance, volume, device, disk_bus, device_type, tag=tag, supports_multiattach=supports_multiattach, delete_on_termination=delete_on_termination)

    def _detach_volume_shelved_offloaded(self, context, instance, volume):
        """Detach a volume from an instance in shelved offloaded state.

        If the instance is shelved offloaded we just need to cleanup volume
        calling the volume api detach, the volume api terminate_connection
        and delete the bdm record.
        If the volume has delete_on_termination option set then we call the
        volume api delete as well.
        """

        @wrap_instance_event(prefix='api')
        def detach_volume(self, context, instance, bdms):
            self._local_cleanup_bdm_volumes(bdms, instance, context)
        bdms = [objects.BlockDeviceMapping.get_by_volume_id(context, volume['id'], instance.uuid)]
        if not bdms[0].attachment_id:
            try:
                self.volume_api.begin_detaching(context, volume['id'])
            except exception.InvalidInput as exc:
                raise exception.InvalidVolume(reason=exc.format_message())
        self._record_action_start(context, instance, instance_actions.DETACH_VOLUME)
        detach_volume(self, context, instance, bdms)

    @check_instance_host(check_is_up=True)
    def _detach_volume(self, context, instance, volume):
        try:
            self.volume_api.begin_detaching(context, volume['id'])
        except exception.InvalidInput as exc:
            raise exception.InvalidVolume(reason=exc.format_message())
        attachments = volume.get('attachments', {})
        attachment_id = None
        if attachments and instance.uuid in attachments:
            attachment_id = attachments[instance.uuid]['attachment_id']
        self._record_action_start(context, instance, instance_actions.DETACH_VOLUME)
        self.compute_rpcapi.detach_volume(context, instance=instance, volume_id=volume['id'], attachment_id=attachment_id)

    @check_instance_lock
    @check_instance_state(vm_state=[vm_states.ACTIVE, vm_states.PAUSED, vm_states.STOPPED, vm_states.RESIZED, vm_states.SOFT_DELETED, vm_states.SHELVED, vm_states.SHELVED_OFFLOADED])
    def detach_volume(self, context, instance, volume):
        """Detach a volume from an instance."""
        if instance.vm_state == vm_states.SHELVED_OFFLOADED:
            self._detach_volume_shelved_offloaded(context, instance, volume)
        else:
            self._detach_volume(context, instance, volume)

    def _count_attachments_for_swap(self, ctxt, volume):
        """Counts the number of attachments for a swap-related volume.

        Attempts to only count read/write attachments if the volume attachment
        records exist, otherwise simply just counts the number of attachments
        regardless of attach mode.

        :param ctxt: nova.context.RequestContext - user request context
        :param volume: nova-translated volume dict from nova.volume.cinder.
        :returns: count of attachments for the volume
        """
        attachments = volume.get('attachments', {})
        if len(attachments) > 1:
            count = 0
            for attachment in attachments.values():
                attachment_id = attachment['attachment_id']
                try:
                    attachment_record = self.volume_api.attachment_get(ctxt, attachment_id)
                    if attachment_record['attach_mode'] == 'rw':
                        count += 1
                except exception.VolumeAttachmentNotFound:
                    count += 1
        else:
            count = len(attachments)
        return count

    @check_instance_lock
    @check_instance_state(vm_state=[vm_states.ACTIVE, vm_states.PAUSED, vm_states.RESIZED])
    def swap_volume(self, context, instance, old_volume, new_volume):
        """Swap volume attached to an instance."""
        if not old_volume.get('attachments', {}).get(instance.uuid):
            msg = _('Old volume is attached to a different instance.')
            raise exception.InvalidVolume(reason=msg)
        if new_volume['attach_status'] == 'attached':
            msg = _('New volume must be detached in order to swap.')
            raise exception.InvalidVolume(reason=msg)
        if int(new_volume['size']) < int(old_volume['size']):
            msg = _('New volume must be the same size or larger.')
            raise exception.InvalidVolume(reason=msg)
        self.volume_api.check_availability_zone(context, new_volume, instance=instance)
        try:
            self.volume_api.begin_detaching(context, old_volume['id'])
        except exception.InvalidInput as exc:
            raise exception.InvalidVolume(reason=exc.format_message())
        try:
            if self._count_attachments_for_swap(context, old_volume) > 1:
                raise exception.MultiattachSwapVolumeNotSupported()
        except Exception:
            with excutils.save_and_reraise_exception():
                self.volume_api.roll_detaching(context, old_volume['id'])
        bdm = objects.BlockDeviceMapping.get_by_volume_and_instance(context, old_volume['id'], instance.uuid)
        new_attachment_id = None
        if bdm.attachment_id is None:
            self.volume_api.reserve_volume(context, new_volume['id'])
        else:
            try:
                self._check_volume_already_attached(context, instance, new_volume)
            except exception.InvalidVolume:
                with excutils.save_and_reraise_exception():
                    self.volume_api.roll_detaching(context, old_volume['id'])
            new_attachment_id = self.volume_api.attachment_create(context, new_volume['id'], instance.uuid)['id']
        self._record_action_start(context, instance, instance_actions.SWAP_VOLUME)
        try:
            self.compute_rpcapi.swap_volume(context, instance=instance, old_volume_id=old_volume['id'], new_volume_id=new_volume['id'], new_attachment_id=new_attachment_id)
        except Exception:
            with excutils.save_and_reraise_exception():
                self.volume_api.roll_detaching(context, old_volume['id'])
                if new_attachment_id is None:
                    self.volume_api.unreserve_volume(context, new_volume['id'])
                else:
                    self.volume_api.attachment_delete(context, new_attachment_id)

    @check_instance_lock
    @check_instance_state(vm_state=[vm_states.ACTIVE, vm_states.PAUSED, vm_states.STOPPED], task_state=[None])
    def attach_interface(self, context, instance, network_id, port_id, requested_ip, tag=None):
        """Use hotplug to add an network adapter to an instance."""
        self._record_action_start(context, instance, instance_actions.ATTACH_INTERFACE)
        if port_id:
            port = self.network_api.show_port(context, port_id)['port']
            if port.get(constants.RESOURCE_REQUEST):
                svc = objects.Service.get_by_host_and_binary(context, instance.host, 'nova-compute')
                if svc.version < 55:
                    raise exception.AttachInterfaceWithQoSPolicyNotSupported(instance_uuid=instance.uuid)
            if port.get('binding:vnic_type', 'normal') == 'vdpa':
                raise exception.OperationNotSupportedForVDPAInterface(instance_uuid=instance.uuid, operation=instance_actions.ATTACH_INTERFACE)
        return self.compute_rpcapi.attach_interface(context, instance=instance, network_id=network_id, port_id=port_id, requested_ip=requested_ip, tag=tag)

    @check_instance_lock
    @check_instance_state(vm_state=[vm_states.ACTIVE, vm_states.PAUSED, vm_states.STOPPED], task_state=[None])
    def detach_interface(self, context, instance, port_id):
        """Detach an network adapter from an instance."""
        for vif in instance.get_network_info():
            if vif['id'] == port_id:
                if vif['vnic_type'] == 'vdpa':
                    raise exception.OperationNotSupportedForVDPAInterface(instance_uuid=instance.uuid, operation=instance_actions.DETACH_INTERFACE)
                break
        else:
            port = self.network_api.show_port(context, port_id)['port']
            if port.get('binding:vnic_type', 'normal') == 'vdpa':
                raise exception.OperationNotSupportedForVDPAInterface(instance_uuid=instance.uuid, operation=instance_actions.DETACH_INTERFACE)
        self._record_action_start(context, instance, instance_actions.DETACH_INTERFACE)
        self.compute_rpcapi.detach_interface(context, instance=instance, port_id=port_id)

    def get_instance_metadata(self, context, instance):
        """Get all metadata associated with an instance."""
        return self.db.instance_metadata_get(context, instance.uuid)

    @check_instance_lock
    @check_instance_state(vm_state=[vm_states.ACTIVE, vm_states.PAUSED, vm_states.SUSPENDED, vm_states.STOPPED], task_state=None)
    def delete_instance_metadata(self, context, instance, key):
        """Delete the given metadata item from an instance."""
        instance.delete_metadata_key(key)

    @check_instance_lock
    @check_instance_state(vm_state=[vm_states.ACTIVE, vm_states.PAUSED, vm_states.SUSPENDED, vm_states.STOPPED], task_state=None)
    def update_instance_metadata(self, context, instance, metadata, delete=False):
        """Updates or creates instance metadata.

        If delete is True, metadata items that are not specified in the
        `metadata` argument will be deleted.

        """
        if delete:
            _metadata = metadata
        else:
            _metadata = dict(instance.metadata)
            _metadata.update(metadata)
        self._check_metadata_properties_quota(context, _metadata)
        instance.metadata = _metadata
        instance.save()
        return _metadata

    @reject_vdpa_instances(instance_actions.LIVE_MIGRATION)
    @block_accelerators()
    @reject_vtpm_instances(instance_actions.LIVE_MIGRATION)
    @reject_sev_instances(instance_actions.LIVE_MIGRATION)
    @check_instance_lock
    @check_instance_state(vm_state=[vm_states.ACTIVE, vm_states.PAUSED])
    def live_migrate(self, context, instance, block_migration, disk_over_commit, host_name, force=None, async_=False):
        """Migrate a server lively to a new host."""
        LOG.debug('Going to try to live migrate instance to %s', host_name or 'another host', instance=instance)
        if host_name:
            nodes = objects.ComputeNodeList.get_all_by_host(context, host_name)
        request_spec = objects.RequestSpec.get_by_instance_uuid(context, instance.uuid)
        instance.task_state = task_states.MIGRATING
        instance.save(expected_task_state=[None])
        self._record_action_start(context, instance, instance_actions.LIVE_MIGRATION)
        if force is False and host_name:
            host_name = None
            target = nodes[0]
            destination = objects.Destination(host=target.host, node=target.hypervisor_hostname)
            request_spec.requested_destination = destination
        try:
            self.compute_task_api.live_migrate_instance(context, instance, host_name, block_migration=block_migration, disk_over_commit=disk_over_commit, request_spec=request_spec, async_=async_)
        except oslo_exceptions.MessagingTimeout as messaging_timeout:
            with excutils.save_and_reraise_exception():
                compute_utils.add_instance_fault_from_exc(context, instance, messaging_timeout)

    @check_instance_lock
    @check_instance_state(vm_state=[vm_states.ACTIVE], task_state=[task_states.MIGRATING])
    def live_migrate_force_complete(self, context, instance, migration_id):
        """Force live migration to complete.

        :param context: Security context
        :param instance: The instance that is being migrated
        :param migration_id: ID of ongoing migration

        """
        LOG.debug('Going to try to force live migration to complete', instance=instance)
        migration = objects.Migration.get_by_id_and_instance(context, migration_id, instance.uuid)
        if migration.status != 'running':
            raise exception.InvalidMigrationState(migration_id=migration_id, instance_uuid=instance.uuid, state=migration.status, method='force complete')
        self._record_action_start(context, instance, instance_actions.LIVE_MIGRATION_FORCE_COMPLETE)
        self.compute_rpcapi.live_migration_force_complete(context, instance, migration)

    @check_instance_lock
    @check_instance_state(task_state=[task_states.MIGRATING])
    def live_migrate_abort(self, context, instance, migration_id, support_abort_in_queue=False):
        """Abort an in-progress live migration.

        :param context: Security context
        :param instance: The instance that is being migrated
        :param migration_id: ID of in-progress live migration
        :param support_abort_in_queue: Flag indicating whether we can support
            abort migrations in "queued" or "preparing" status.

        """
        migration = objects.Migration.get_by_id_and_instance(context, migration_id, instance.uuid)
        LOG.debug('Going to cancel live migration %s', migration.id, instance=instance)
        allowed_states = ['running']
        queued_states = ['queued', 'preparing']
        if support_abort_in_queue:
            allowed_states.extend(queued_states)
        if migration.status not in allowed_states:
            raise exception.InvalidMigrationState(migration_id=migration_id, instance_uuid=instance.uuid, state=migration.status, method='abort live migration')
        self._record_action_start(context, instance, instance_actions.LIVE_MIGRATION_CANCEL)
        self.compute_rpcapi.live_migration_abort(context, instance, migration.id)

    @reject_vdpa_instances(instance_actions.EVACUATE)
    @reject_vtpm_instances(instance_actions.EVACUATE)
    @block_accelerators(until_service=SUPPORT_ACCELERATOR_SERVICE_FOR_REBUILD)
    @check_instance_state(vm_state=[vm_states.ACTIVE, vm_states.STOPPED, vm_states.ERROR])
    def evacuate(self, context, instance, host, on_shared_storage, admin_password=None, force=None):
        """Running evacuate to target host.

        Checking vm compute host state, if the host not in expected_state,
        raising an exception.

        :param instance: The instance to evacuate
        :param host: Target host. if not set, the scheduler will pick up one
        :param on_shared_storage: True if instance files on shared storage
        :param admin_password: password to set on rebuilt instance
        :param force: Force the evacuation to the specific host target

        """
        LOG.debug('vm evacuation scheduled', instance=instance)
        inst_host = instance.host
        service = objects.Service.get_by_compute_host(context, inst_host)
        if self.servicegroup_api.service_is_up(service):
            LOG.error('Instance compute service state on %s expected to be down, but it was up.', inst_host)
            raise exception.ComputeServiceInUse(host=inst_host)
        request_spec = objects.RequestSpec.get_by_instance_uuid(context, instance.uuid)
        instance.task_state = task_states.REBUILDING
        instance.save(expected_task_state=[None])
        self._record_action_start(context, instance, instance_actions.EVACUATE)
        migration = objects.Migration(context, source_compute=instance.host, source_node=instance.node, instance_uuid=instance.uuid, status='accepted', migration_type=fields_obj.MigrationType.EVACUATION)
        if host:
            migration.dest_compute = host
        migration.create()
        compute_utils.notify_about_instance_usage(self.notifier, context, instance, 'evacuate')
        compute_utils.notify_about_instance_action(context, instance, CONF.host, action=fields_obj.NotificationAction.EVACUATE, source=fields_obj.NotificationSource.API)
        if force is False and host:
            nodes = objects.ComputeNodeList.get_all_by_host(context, host)
            host = None
            target = nodes[0]
            destination = objects.Destination(host=target.host, node=target.hypervisor_hostname)
            request_spec.requested_destination = destination
        return self.compute_task_api.rebuild_instance(context, instance=instance, new_pass=admin_password, injected_files=None, image_ref=None, orig_image_ref=None, orig_sys_metadata=None, bdms=None, recreate=True, on_shared_storage=on_shared_storage, host=host, request_spec=request_spec)

    def get_migrations(self, context, filters):
        """Get all migrations for the given filters."""
        load_cells()
        migrations = []
        for cell in CELLS:
            if cell.uuid == objects.CellMapping.CELL0_UUID:
                continue
            with nova_context.target_cell(context, cell) as cctxt:
                migrations.extend(objects.MigrationList.get_by_filters(cctxt, filters).objects)
        return objects.MigrationList(objects=migrations)

    def get_migrations_sorted(self, context, filters, sort_dirs=None, sort_keys=None, limit=None, marker=None):
        """Get all migrations for the given parameters."""
        mig_objs = migration_list.get_migration_objects_sorted(context, filters, limit, marker, sort_keys, sort_dirs)

        def _get_newer_obj(obj1, obj2):
            created_at1 = obj1.created_at
            created_at2 = obj2.created_at
            updated_at1 = obj1.updated_at
            updated_at2 = obj2.updated_at
            if updated_at1 and updated_at2:
                if updated_at1 > updated_at2:
                    return obj1
                return obj2
            if updated_at1:
                if updated_at1 > created_at2:
                    return obj1
                return obj2
            if updated_at2:
                if updated_at2 > created_at1:
                    return obj2
                return obj1
            if created_at1 > created_at2:
                return obj1
            return obj2
        migrations_by_uuid = collections.OrderedDict()
        for migration in mig_objs:
            if migration.uuid not in migrations_by_uuid:
                migrations_by_uuid[migration.uuid] = migration
            else:
                doppelganger = migrations_by_uuid[migration.uuid]
                newer = _get_newer_obj(doppelganger, migration)
                migrations_by_uuid[migration.uuid] = newer
        return objects.MigrationList(objects=list(migrations_by_uuid.values()))

    def get_migrations_in_progress_by_instance(self, context, instance_uuid, migration_type=None):
        """Get all migrations of an instance in progress."""
        return objects.MigrationList.get_in_progress_by_instance(context, instance_uuid, migration_type)

    def get_migration_by_id_and_instance(self, context, migration_id, instance_uuid):
        """Get the migration of an instance by id."""
        return objects.Migration.get_by_id_and_instance(context, migration_id, instance_uuid)

    def _get_bdm_by_volume_id(self, context, volume_id, expected_attrs=None):
        """Retrieve a BDM without knowing its cell.

        .. note:: The context will be targeted to the cell in which the
            BDM is found, if any.

        :param context: The API request context.
        :param volume_id: The ID of the volume.
        :param expected_attrs: list of any additional attributes that should
            be joined when the BDM is loaded from the database.
        :raises: nova.exception.VolumeBDMNotFound if not found in any cell
        """
        load_cells()
        for cell in CELLS:
            nova_context.set_target_cell(context, cell)
            try:
                return objects.BlockDeviceMapping.get_by_volume(context, volume_id, expected_attrs=expected_attrs)
            except exception.NotFound:
                continue
        raise exception.VolumeBDMNotFound(volume_id=volume_id)

    def volume_snapshot_create(self, context, volume_id, create_info):
        bdm = self._get_bdm_by_volume_id(context, volume_id, expected_attrs=['instance'])

        @check_instance_host()
        @check_instance_state(vm_state=None)
        def do_volume_snapshot_create(self, context, instance):
            self.compute_rpcapi.volume_snapshot_create(context, instance, volume_id, create_info)
            snapshot = {'snapshot': {'id': create_info.get('id'), 'volumeId': volume_id}}
            return snapshot
        return do_volume_snapshot_create(self, context, bdm.instance)

    def volume_snapshot_delete(self, context, volume_id, snapshot_id, delete_info):
        bdm = self._get_bdm_by_volume_id(context, volume_id, expected_attrs=['instance'])

        @check_instance_host()
        @check_instance_state(vm_state=None)
        def do_volume_snapshot_delete(self, context, instance):
            if delete_info.get('merge_target_file') is not None and instance.vm_state != vm_states.ACTIVE:
                raise exception.InstanceInvalidState(attr='vm_state', instance_uuid=instance.uuid, state=instance.vm_state, method='volume_snapshot_delete')
            self.compute_rpcapi.volume_snapshot_delete(context, instance, volume_id, snapshot_id, delete_info)
        do_volume_snapshot_delete(self, context, bdm.instance)

    def external_instance_event(self, api_context, instances, events):
        instances_by_host = collections.defaultdict(list)
        events_by_host = collections.defaultdict(list)
        hosts_by_instance = collections.defaultdict(list)
        cell_contexts_by_host = {}
        for instance in instances:
            (hosts, cross_cell_move) = self._get_relevant_hosts(instance._context, instance)
            for host in hosts:
                if host not in cell_contexts_by_host:
                    if cross_cell_move:
                        hm = objects.HostMapping.get_by_host(api_context, host)
                        ctxt = nova_context.get_admin_context()
                        nova_context.set_target_cell(ctxt, hm.cell_mapping)
                        cell_contexts_by_host[host] = ctxt
                    else:
                        cell_contexts_by_host[host] = instance._context
                instances_by_host[host].append(instance)
                hosts_by_instance[instance.uuid].append(host)
        for event in events:
            if event.name == 'volume-extended':
                host = hosts_by_instance[event.instance_uuid][0]
                cell_context = cell_contexts_by_host[host]
                objects.InstanceAction.action_start(cell_context, event.instance_uuid, instance_actions.EXTEND_VOLUME, want_result=False)
            elif event.name == 'power-update':
                host = hosts_by_instance[event.instance_uuid][0]
                cell_context = cell_contexts_by_host[host]
                if event.tag == external_event_obj.POWER_ON:
                    inst_action = instance_actions.START
                elif event.tag == external_event_obj.POWER_OFF:
                    inst_action = instance_actions.STOP
                else:
                    LOG.warning('Invalid power state %s. Cannot process the event %s. Skipping it.', event.tag, event)
                    continue
                objects.InstanceAction.action_start(cell_context, event.instance_uuid, inst_action, want_result=False)
            for host in hosts_by_instance[event.instance_uuid]:
                events_by_host[host].append(event)
        for host in instances_by_host:
            cell_context = cell_contexts_by_host[host]
            self.compute_rpcapi.external_instance_event(cell_context, instances_by_host[host], events_by_host[host], host=host)

    def _get_relevant_hosts(self, context, instance):
        """Get the relevant hosts for an external server event on an instance.

        :param context: nova auth request context targeted at the same cell
            that the instance lives in
        :param instance: Instance object which is the target of an external
            server event
        :returns: 2-item tuple of:
            - set of at least one host (the host where the instance lives); if
              the instance is being migrated the source and dest compute
              hostnames are in the returned set
            - boolean indicating if the instance is being migrated across cells
        """
        hosts = set()
        hosts.add(instance.host)
        cross_cell_move = False
        if instance.migration_context is not None:
            migration_id = instance.migration_context.migration_id
            migration = objects.Migration.get_by_id(context, migration_id)
            cross_cell_move = migration.cross_cell_move
            hosts.add(migration.dest_compute)
            hosts.add(migration.source_compute)
            cells_msg = 'across cells' if cross_cell_move else 'within the same cell'
            LOG.debug('Instance %(instance)s is migrating %(cells_msg)s, copying events to all relevant hosts: %(hosts)s', {'cells_msg': cells_msg, 'instance': instance.uuid, 'hosts': hosts})
        return (hosts, cross_cell_move)

    def get_instance_host_status(self, instance):
        if instance.host:
            try:
                service = [service for service in instance.services if service.binary == 'nova-compute'][0]
                if service.forced_down:
                    host_status = fields_obj.HostStatus.DOWN
                elif service.disabled:
                    host_status = fields_obj.HostStatus.MAINTENANCE
                else:
                    alive = self.servicegroup_api.service_is_up(service)
                    host_status = alive and fields_obj.HostStatus.UP or fields_obj.HostStatus.UNKNOWN
            except IndexError:
                host_status = fields_obj.HostStatus.NONE
        else:
            host_status = fields_obj.HostStatus.NONE
        return host_status

    def get_instances_host_statuses(self, instance_list):
        host_status_dict = dict()
        host_statuses = dict()
        for instance in instance_list:
            if instance.host:
                if instance.host not in host_status_dict:
                    host_status = self.get_instance_host_status(instance)
                    host_status_dict[instance.host] = host_status
                else:
                    host_status = host_status_dict[instance.host]
            else:
                host_status = fields_obj.HostStatus.NONE
            host_statuses[instance.uuid] = host_status
        return host_statuses

@p.trace('target_host_cell')
def target_host_cell(fn):
    """Target a host-based function to a cell.

    Expects to wrap a function of signature:

       func(self, context, host, ...)
    """

    @functools.wraps(fn)
    def targeted(self, context, host, *args, **kwargs):
        mapping = objects.HostMapping.get_by_host(context, host)
        nova_context.set_target_cell(context, mapping.cell_mapping)
        return fn(self, context, host, *args, **kwargs)
    return targeted

@p.trace('_get_service_in_cell_by_host')
def _get_service_in_cell_by_host(context, host_name):
    try:
        mapping = objects.HostMapping.get_by_host(context, host_name)
        nova_context.set_target_cell(context, mapping.cell_mapping)
        service = objects.Service.get_by_compute_host(context, host_name)
    except exception.HostMappingNotFound:
        try:
            service = _find_service_in_cell(context, service_host=host_name)
        except exception.NotFound:
            raise exception.ComputeHostNotFound(host=host_name)
    return service

@p.trace('_find_service_in_cell')
def _find_service_in_cell(context, service_id=None, service_host=None):
    """Find a service by id or hostname by searching all cells.

    If one matching service is found, return it. If none or multiple
    are found, raise an exception.

    :param context: A context.RequestContext
    :param service_id: If not none, the DB ID of the service to find
    :param service_host: If not None, the hostname of the service to find
    :returns: An objects.Service
    :raises: ServiceNotUnique if multiple matching IDs are found
    :raises: NotFound if no matches are found
    :raises: NovaException if called with neither search option
    """
    load_cells()
    service = None
    found_in_cell = None
    is_uuid = False
    if service_id is not None:
        is_uuid = uuidutils.is_uuid_like(service_id)
        if is_uuid:
            lookup_fn = lambda c: objects.Service.get_by_uuid(c, service_id)
        else:
            lookup_fn = lambda c: objects.Service.get_by_id(c, service_id)
    elif service_host is not None:
        lookup_fn = lambda c: objects.Service.get_by_compute_host(c, service_host)
    else:
        LOG.exception('_find_service_in_cell called with no search parameters')
        raise exception.NovaException()
    for cell in CELLS:
        try:
            with nova_context.target_cell(context, cell) as cctxt:
                cell_service = lookup_fn(cctxt)
        except exception.NotFound:
            continue
        if service and cell_service:
            raise exception.ServiceNotUnique()
        service = cell_service
        found_in_cell = cell
        if service and is_uuid:
            break
    if service:
        nova_context.set_target_cell(context, found_in_cell)
        return service
    else:
        raise exception.NotFound()

@p.trace_cls('HostAPI')
class HostAPI(base.Base):
    """Sub-set of the Compute Manager API for managing host operations."""

    def __init__(self, rpcapi=None, servicegroup_api=None):
        self.rpcapi = rpcapi or compute_rpcapi.ComputeAPI()
        self.servicegroup_api = servicegroup_api or servicegroup.API()
        super(HostAPI, self).__init__()

    def _assert_host_exists(self, context, host_name, must_be_up=False):
        """Raise HostNotFound if compute host doesn't exist."""
        service = objects.Service.get_by_compute_host(context, host_name)
        if not service:
            raise exception.HostNotFound(host=host_name)
        if must_be_up and (not self.servicegroup_api.service_is_up(service)):
            raise exception.ComputeServiceUnavailable(host=host_name)
        return service['host']

    @wrap_exception()
    @target_host_cell
    def set_host_enabled(self, context, host_name, enabled):
        """Sets the specified host's ability to accept new instances."""
        host_name = self._assert_host_exists(context, host_name)
        payload = {'host_name': host_name, 'enabled': enabled}
        compute_utils.notify_about_host_update(context, 'set_enabled.start', payload)
        result = self.rpcapi.set_host_enabled(context, enabled=enabled, host=host_name)
        compute_utils.notify_about_host_update(context, 'set_enabled.end', payload)
        return result

    @target_host_cell
    def get_host_uptime(self, context, host_name):
        """Returns the result of calling "uptime" on the target host."""
        host_name = self._assert_host_exists(context, host_name, must_be_up=True)
        return self.rpcapi.get_host_uptime(context, host=host_name)

    @wrap_exception()
    @target_host_cell
    def host_power_action(self, context, host_name, action):
        """Reboots, shuts down or powers up the host."""
        host_name = self._assert_host_exists(context, host_name)
        payload = {'host_name': host_name, 'action': action}
        compute_utils.notify_about_host_update(context, 'power_action.start', payload)
        result = self.rpcapi.host_power_action(context, action=action, host=host_name)
        compute_utils.notify_about_host_update(context, 'power_action.end', payload)
        return result

    @wrap_exception()
    @target_host_cell
    def set_host_maintenance(self, context, host_name, mode):
        """Start/Stop host maintenance window. On start, it triggers
        guest VMs evacuation.
        """
        host_name = self._assert_host_exists(context, host_name)
        payload = {'host_name': host_name, 'mode': mode}
        compute_utils.notify_about_host_update(context, 'set_maintenance.start', payload)
        result = self.rpcapi.host_maintenance_mode(context, host_param=host_name, mode=mode, host=host_name)
        compute_utils.notify_about_host_update(context, 'set_maintenance.end', payload)
        return result

    def service_get_all(self, context, filters=None, set_zones=False, all_cells=False, cell_down_support=False):
        """Returns a list of services, optionally filtering the results.

        If specified, 'filters' should be a dictionary containing services
        attributes and matching values.  Ie, to get a list of services for
        the 'compute' topic, use filters={'topic': 'compute'}.

        If all_cells=True, then scan all cells and merge the results.

        If cell_down_support=True then return minimal service records
        for cells that do not respond based on what we have in the
        host mappings. These will have only 'binary' and 'host' set.
        """
        if filters is None:
            filters = {}
        disabled = filters.pop('disabled', None)
        if 'availability_zone' in filters:
            set_zones = True
        if all_cells:
            services = []
            service_dict = nova_context.scatter_gather_all_cells(context, objects.ServiceList.get_all, disabled, set_zones=set_zones)
            for (cell_uuid, service) in service_dict.items():
                if not nova_context.is_cell_failure_sentinel(service):
                    services.extend(service)
                elif cell_down_support:
                    unavailable_services = objects.ServiceList()
                    cid = [cm.id for cm in nova_context.CELLS if cm.uuid == cell_uuid]
                    hms = objects.HostMappingList.get_by_cell_id(context, cid[0])
                    for hm in hms:
                        unavailable_services.objects.append(objects.Service(binary='nova-compute', host=hm.host))
                    LOG.warning('Cell %s is not responding and hence only partial results are available from this cell.', cell_uuid)
                    services.extend(unavailable_services)
                else:
                    LOG.warning('Cell %s is not responding and hence skipped from the results.', cell_uuid)
        else:
            services = objects.ServiceList.get_all(context, disabled, set_zones=set_zones)
        ret_services = []
        for service in services:
            for (key, val) in filters.items():
                if service[key] != val:
                    break
            else:
                ret_services.append(service)
        return ret_services

    def service_get_by_id(self, context, service_id):
        """Get service entry for the given service id or uuid."""
        try:
            return _find_service_in_cell(context, service_id=service_id)
        except exception.NotFound:
            raise exception.ServiceNotFound(service_id=service_id)

    @target_host_cell
    def service_get_by_compute_host(self, context, host_name):
        """Get service entry for the given compute hostname."""
        return objects.Service.get_by_compute_host(context, host_name)

    def _update_compute_provider_status(self, context, service):
        """Calls the compute service to sync the COMPUTE_STATUS_DISABLED trait.

        There are two cases where the API will not call the compute service:

        * The compute service is down. In this case the trait is synchronized
          when the compute service is restarted.
        * The compute service is old. In this case the trait is synchronized
          when the compute service is upgraded and restarted.

        :param context: nova auth RequestContext
        :param service: nova.objects.Service object which has been enabled
            or disabled (see ``service_update``).
        """
        if not self.servicegroup_api.service_is_up(service):
            LOG.info('Compute service on host %s is down. The COMPUTE_STATUS_DISABLED trait will be synchronized when the service is restarted.', service.host)
            return
        if service.version < MIN_COMPUTE_SYNC_COMPUTE_STATUS_DISABLED:
            LOG.info('Compute service on host %s is too old to sync the COMPUTE_STATUS_DISABLED trait in Placement. The trait will be synchronized when the service is upgraded and restarted.', service.host)
            return
        enabled = not service.disabled
        try:
            LOG.debug('Calling the compute service on host %s to sync the COMPUTE_STATUS_DISABLED trait.', service.host)
            self.rpcapi.set_host_enabled(context, service.host, enabled)
        except Exception:
            LOG.exception('An error occurred while updating the COMPUTE_STATUS_DISABLED trait on compute node resource providers managed by host %s. The trait will be synchronized automatically by the compute service when the update_available_resource periodic task runs.', service.host)

    def service_update(self, context, service):
        """Performs the actual service update operation.

        If the "disabled" field is changed, potentially calls the compute
        service to sync the COMPUTE_STATUS_DISABLED trait on the compute node
        resource providers managed by this compute service.

        :param context: nova auth RequestContext
        :param service: nova.objects.Service object with changes already
            set on the object
        """
        update_placement = 'disabled' in service.obj_what_changed()
        service.save()
        if update_placement:
            self._update_compute_provider_status(context, service)
        return service

    @target_host_cell
    def service_update_by_host_and_binary(self, context, host_name, binary, params_to_update):
        """Enable / Disable a service.

        Determines the cell that the service is in using the HostMapping.

        For compute services, this stops new builds and migrations going to
        the host.

        See also ``service_update``.

        :param context: nova auth RequestContext
        :param host_name: hostname of the service
        :param binary: service binary (really only supports "nova-compute")
        :param params_to_update: dict of changes to make to the Service object
        :raises: HostMappingNotFound if the host is not mapped to a cell
        :raises: HostBinaryNotFound if a services table record is not found
            with the given host_name and binary
        """
        service = objects.Service.get_by_args(context, host_name, binary)
        service.update(params_to_update)
        return self.service_update(context, service)

    @target_host_cell
    def instance_get_all_by_host(self, context, host_name):
        """Return all instances on the given host."""
        return objects.InstanceList.get_by_host(context, host_name)

    def task_log_get_all(self, context, task_name, period_beginning, period_ending, host=None, state=None):
        """Return the task logs within a given range, optionally
        filtering by host and/or state.
        """
        return self.db.task_log_get_all(context, task_name, period_beginning, period_ending, host=host, state=state)

    def compute_node_get(self, context, compute_id):
        """Return compute node entry for particular integer ID or UUID."""
        load_cells()
        is_uuid = uuidutils.is_uuid_like(compute_id)
        for cell in CELLS:
            if cell.uuid == objects.CellMapping.CELL0_UUID:
                continue
            with nova_context.target_cell(context, cell) as cctxt:
                try:
                    if is_uuid:
                        return objects.ComputeNode.get_by_uuid(cctxt, compute_id)
                    return objects.ComputeNode.get_by_id(cctxt, int(compute_id))
                except exception.ComputeHostNotFound:
                    continue
        raise exception.ComputeHostNotFound(host=compute_id)

    def compute_node_get_all(self, context, limit=None, marker=None):
        load_cells()
        computes = []
        uuid_marker = marker and uuidutils.is_uuid_like(marker)
        for cell in CELLS:
            if cell.uuid == objects.CellMapping.CELL0_UUID:
                continue
            with nova_context.target_cell(context, cell) as cctxt:
                if marker and uuid_marker:
                    try:
                        compute_marker = objects.ComputeNode.get_by_uuid(cctxt, marker)
                        marker = compute_marker.id
                    except exception.ComputeHostNotFound:
                        continue
                try:
                    cell_computes = objects.ComputeNodeList.get_by_pagination(cctxt, limit=limit, marker=marker)
                except exception.MarkerNotFound:
                    continue
                computes.extend(cell_computes)
                marker = None
                if limit:
                    limit -= len(cell_computes)
                    if limit <= 0:
                        break
        if marker is not None and len(computes) == 0:
            raise exception.MarkerNotFound(marker=marker)
        return objects.ComputeNodeList(objects=computes)

    def compute_node_search_by_hypervisor(self, context, hypervisor_match):
        load_cells()
        computes = []
        for cell in CELLS:
            if cell.uuid == objects.CellMapping.CELL0_UUID:
                continue
            with nova_context.target_cell(context, cell) as cctxt:
                cell_computes = objects.ComputeNodeList.get_by_hypervisor(cctxt, hypervisor_match)
            computes.extend(cell_computes)
        return objects.ComputeNodeList(objects=computes)

    def compute_node_statistics(self, context):
        load_cells()
        cell_stats = []
        for cell in CELLS:
            if cell.uuid == objects.CellMapping.CELL0_UUID:
                continue
            with nova_context.target_cell(context, cell) as cctxt:
                cell_stats.append(self.db.compute_node_statistics(cctxt))
        if cell_stats:
            keys = cell_stats[0].keys()
            return {k: sum((stats[k] for stats in cell_stats)) for k in keys}
        else:
            return {}

@p.trace_cls('InstanceActionAPI')
class InstanceActionAPI(base.Base):
    """Sub-set of the Compute Manager API for managing instance actions."""

    def actions_get(self, context, instance, limit=None, marker=None, filters=None):
        return objects.InstanceActionList.get_by_instance_uuid(context, instance.uuid, limit, marker, filters)

    def action_get_by_request_id(self, context, instance, request_id):
        return objects.InstanceAction.get_by_request_id(context, instance.uuid, request_id)

    def action_events_get(self, context, instance, action_id):
        return objects.InstanceActionEventList.get_by_action(context, action_id)

@p.trace_cls('AggregateAPI')
class AggregateAPI(base.Base):
    """Sub-set of the Compute Manager API for managing host aggregates."""

    def __init__(self, **kwargs):
        self.compute_rpcapi = compute_rpcapi.ComputeAPI()
        self.query_client = query.SchedulerQueryClient()
        self._placement_client = None
        super(AggregateAPI, self).__init__(**kwargs)

    @property
    def placement_client(self):
        if self._placement_client is None:
            self._placement_client = report.SchedulerReportClient()
        return self._placement_client

    @wrap_exception()
    def create_aggregate(self, context, aggregate_name, availability_zone):
        """Creates the model for the aggregate."""
        aggregate = objects.Aggregate(context=context)
        aggregate.name = aggregate_name
        if availability_zone:
            aggregate.metadata = {'availability_zone': availability_zone}
        aggregate.create()
        self.query_client.update_aggregates(context, [aggregate])
        return aggregate

    def get_aggregate(self, context, aggregate_id):
        """Get an aggregate by id."""
        return objects.Aggregate.get_by_id(context, aggregate_id)

    def get_aggregate_list(self, context):
        """Get all the aggregates."""
        return objects.AggregateList.get_all(context)

    def get_aggregates_by_host(self, context, compute_host):
        """Get all the aggregates where the given host is presented."""
        return objects.AggregateList.get_by_host(context, compute_host)

    @wrap_exception()
    def update_aggregate(self, context, aggregate_id, values):
        """Update the properties of an aggregate."""
        aggregate = objects.Aggregate.get_by_id(context, aggregate_id)
        if 'name' in values:
            aggregate.name = values.pop('name')
            aggregate.save()
        self.is_safe_to_update_az(context, values, aggregate=aggregate, action_name=AGGREGATE_ACTION_UPDATE, check_no_instances_in_az=True)
        if values:
            aggregate.update_metadata(values)
            aggregate.updated_at = timeutils.utcnow()
        self.query_client.update_aggregates(context, [aggregate])
        if values.get('availability_zone'):
            availability_zones.reset_cache()
        return aggregate

    @wrap_exception()
    def update_aggregate_metadata(self, context, aggregate_id, metadata):
        """Updates the aggregate metadata."""
        aggregate = objects.Aggregate.get_by_id(context, aggregate_id)
        self.is_safe_to_update_az(context, metadata, aggregate=aggregate, action_name=AGGREGATE_ACTION_UPDATE_META, check_no_instances_in_az=True)
        aggregate.update_metadata(metadata)
        self.query_client.update_aggregates(context, [aggregate])
        if metadata and metadata.get('availability_zone'):
            availability_zones.reset_cache()
        aggregate.updated_at = timeutils.utcnow()
        return aggregate

    @wrap_exception()
    def delete_aggregate(self, context, aggregate_id):
        """Deletes the aggregate."""
        aggregate_payload = {'aggregate_id': aggregate_id}
        compute_utils.notify_about_aggregate_update(context, 'delete.start', aggregate_payload)
        aggregate = objects.Aggregate.get_by_id(context, aggregate_id)
        compute_utils.notify_about_aggregate_action(context=context, aggregate=aggregate, action=fields_obj.NotificationAction.DELETE, phase=fields_obj.NotificationPhase.START)
        if len(aggregate.hosts) > 0:
            msg = _('Host aggregate is not empty')
            raise exception.InvalidAggregateActionDelete(aggregate_id=aggregate_id, reason=msg)
        aggregate.destroy()
        self.query_client.delete_aggregate(context, aggregate)
        compute_utils.notify_about_aggregate_update(context, 'delete.end', aggregate_payload)
        compute_utils.notify_about_aggregate_action(context=context, aggregate=aggregate, action=fields_obj.NotificationAction.DELETE, phase=fields_obj.NotificationPhase.END)

    def is_safe_to_update_az(self, context, metadata, aggregate, hosts=None, action_name=AGGREGATE_ACTION_ADD, check_no_instances_in_az=False):
        """Determine if updates alter an aggregate's availability zone.

            :param context: local context
            :param metadata: Target metadata for updating aggregate
            :param aggregate: Aggregate to update
            :param hosts: Hosts to check. If None, aggregate.hosts is used
            :type hosts: list
            :param action_name: Calling method for logging purposes
            :param check_no_instances_in_az: if True, it checks
                there is no instances on any hosts of the aggregate

        """
        if 'availability_zone' in metadata:
            if not metadata['availability_zone']:
                msg = _('Aggregate %s does not support empty named availability zone') % aggregate.name
                self._raise_invalid_aggregate_exc(action_name, aggregate.id, msg)
            _hosts = hosts or aggregate.hosts
            host_aggregates = objects.AggregateList.get_by_metadata_key(context, 'availability_zone', hosts=_hosts)
            conflicting_azs = [agg.availability_zone for agg in host_aggregates if agg.availability_zone != metadata['availability_zone'] and agg.id != aggregate.id]
            if conflicting_azs:
                msg = _('One or more hosts already in availability zone(s) %s') % conflicting_azs
                self._raise_invalid_aggregate_exc(action_name, aggregate.id, msg)
            same_az_name = aggregate.availability_zone == metadata['availability_zone']
            if check_no_instances_in_az and (not same_az_name):
                instance_count_by_cell = nova_context.scatter_gather_skip_cell0(context, objects.InstanceList.get_count_by_hosts, _hosts)
                if any((cnt for cnt in instance_count_by_cell.values())):
                    msg = _('One or more hosts contain instances in this zone')
                    self._raise_invalid_aggregate_exc(action_name, aggregate.id, msg)

    def _raise_invalid_aggregate_exc(self, action_name, aggregate_id, reason):
        if action_name == AGGREGATE_ACTION_ADD:
            raise exception.InvalidAggregateActionAdd(aggregate_id=aggregate_id, reason=reason)
        elif action_name == AGGREGATE_ACTION_UPDATE:
            raise exception.InvalidAggregateActionUpdate(aggregate_id=aggregate_id, reason=reason)
        elif action_name == AGGREGATE_ACTION_UPDATE_META:
            raise exception.InvalidAggregateActionUpdateMeta(aggregate_id=aggregate_id, reason=reason)
        elif action_name == AGGREGATE_ACTION_DELETE:
            raise exception.InvalidAggregateActionDelete(aggregate_id=aggregate_id, reason=reason)
        raise exception.NovaException(_('Unexpected aggregate action %s') % action_name)

    def _update_az_cache_for_host(self, context, host_name, aggregate_meta):
        if aggregate_meta and aggregate_meta.get('availability_zone'):
            availability_zones.update_host_availability_zone_cache(context, host_name)

    @wrap_exception()
    def add_host_to_aggregate(self, context, aggregate_id, host_name):
        """Adds the host to an aggregate."""
        aggregate_payload = {'aggregate_id': aggregate_id, 'host_name': host_name}
        compute_utils.notify_about_aggregate_update(context, 'addhost.start', aggregate_payload)
        service = _get_service_in_cell_by_host(context, host_name)
        if service.host != host_name:
            raise exception.ComputeHostNotFound(host=host_name)
        aggregate = objects.Aggregate.get_by_id(context, aggregate_id)
        compute_utils.notify_about_aggregate_action(context=context, aggregate=aggregate, action=fields_obj.NotificationAction.ADD_HOST, phase=fields_obj.NotificationPhase.START)
        self.is_safe_to_update_az(context, aggregate.metadata, hosts=[host_name], aggregate=aggregate)
        aggregate.add_host(host_name)
        self.query_client.update_aggregates(context, [aggregate])
        nodes = objects.ComputeNodeList.get_all_by_host(context, host_name)
        node_name = nodes[0].hypervisor_hostname
        try:
            self.placement_client.aggregate_add_host(context, aggregate.uuid, host_name=node_name)
        except (exception.ResourceProviderNotFound, exception.ResourceProviderAggregateRetrievalFailed, exception.ResourceProviderUpdateFailed, exception.ResourceProviderUpdateConflict) as err:
            LOG.warning('Failed to associate %s with a placement aggregate: %s. This may be corrected after running nova-manage placement sync_aggregates.', node_name, err)
        self._update_az_cache_for_host(context, host_name, aggregate.metadata)
        aggregate_payload.update({'name': aggregate.name})
        compute_utils.notify_about_aggregate_update(context, 'addhost.end', aggregate_payload)
        compute_utils.notify_about_aggregate_action(context=context, aggregate=aggregate, action=fields_obj.NotificationAction.ADD_HOST, phase=fields_obj.NotificationPhase.END)
        return aggregate

    @wrap_exception()
    def remove_host_from_aggregate(self, context, aggregate_id, host_name):
        """Removes host from the aggregate."""
        aggregate_payload = {'aggregate_id': aggregate_id, 'host_name': host_name}
        compute_utils.notify_about_aggregate_update(context, 'removehost.start', aggregate_payload)
        _get_service_in_cell_by_host(context, host_name)
        aggregate = objects.Aggregate.get_by_id(context, aggregate_id)
        compute_utils.notify_about_aggregate_action(context=context, aggregate=aggregate, action=fields_obj.NotificationAction.REMOVE_HOST, phase=fields_obj.NotificationPhase.START)
        nodes = objects.ComputeNodeList.get_all_by_host(context, host_name)
        node_name = nodes[0].hypervisor_hostname
        try:
            self.placement_client.aggregate_remove_host(context, aggregate.uuid, node_name)
        except exception.ResourceProviderNotFound as err:
            LOG.warning('Failed to remove association of %s with a placement aggregate: %s.', node_name, err)
        aggregate.delete_host(host_name)
        self.query_client.update_aggregates(context, [aggregate])
        self._update_az_cache_for_host(context, host_name, aggregate.metadata)
        compute_utils.notify_about_aggregate_update(context, 'removehost.end', aggregate_payload)
        compute_utils.notify_about_aggregate_action(context=context, aggregate=aggregate, action=fields_obj.NotificationAction.REMOVE_HOST, phase=fields_obj.NotificationPhase.END)
        return aggregate

@p.trace_cls('KeypairAPI')
class KeypairAPI(base.Base):
    """Subset of the Compute Manager API for managing key pairs."""
    wrap_exception = functools.partial(exception_wrapper.wrap_exception, service='api', binary='nova-api')

    def __init__(self):
        super().__init__()
        self.notifier = rpc.get_notifier('api')

    def _notify(self, context, event_suffix, keypair_name):
        payload = {'tenant_id': context.project_id, 'user_id': context.user_id, 'key_name': keypair_name}
        self.notifier.info(context, 'keypair.%s' % event_suffix, payload)

    def _validate_new_key_pair(self, context, user_id, key_name, key_type):
        safe_chars = '_- ' + string.digits + string.ascii_letters
        clean_value = ''.join((x for x in key_name if x in safe_chars))
        if clean_value != key_name:
            raise exception.InvalidKeypair(reason=_('Keypair name contains unsafe characters'))
        try:
            utils.check_string_length(key_name, min_length=1, max_length=255)
        except exception.InvalidInput:
            raise exception.InvalidKeypair(reason=_('Keypair name must be string and between 1 and 255 characters long'))
        try:
            objects.Quotas.check_deltas(context, {'key_pairs': 1}, user_id)
        except exception.OverQuota:
            raise exception.KeypairLimitExceeded()

    @wrap_exception()
    def import_key_pair(self, context, user_id, key_name, public_key, key_type=keypair_obj.KEYPAIR_TYPE_SSH):
        """Import a key pair using an existing public key."""
        self._validate_new_key_pair(context, user_id, key_name, key_type)
        self._notify(context, 'import.start', key_name)
        keypair = objects.KeyPair(context)
        keypair.user_id = user_id
        keypair.name = key_name
        keypair.type = key_type
        keypair.fingerprint = None
        keypair.public_key = public_key
        compute_utils.notify_about_keypair_action(context=context, keypair=keypair, action=fields_obj.NotificationAction.IMPORT, phase=fields_obj.NotificationPhase.START)
        fingerprint = self._generate_fingerprint(public_key, key_type)
        keypair.fingerprint = fingerprint
        keypair.create()
        compute_utils.notify_about_keypair_action(context=context, keypair=keypair, action=fields_obj.NotificationAction.IMPORT, phase=fields_obj.NotificationPhase.END)
        self._notify(context, 'import.end', key_name)
        return keypair

    @wrap_exception()
    def create_key_pair(self, context, user_id, key_name, key_type=keypair_obj.KEYPAIR_TYPE_SSH):
        """Create a new key pair."""
        self._validate_new_key_pair(context, user_id, key_name, key_type)
        keypair = objects.KeyPair(context)
        keypair.user_id = user_id
        keypair.name = key_name
        keypair.type = key_type
        keypair.fingerprint = None
        keypair.public_key = None
        self._notify(context, 'create.start', key_name)
        compute_utils.notify_about_keypair_action(context=context, keypair=keypair, action=fields_obj.NotificationAction.CREATE, phase=fields_obj.NotificationPhase.START)
        (private_key, public_key, fingerprint) = self._generate_key_pair(user_id, key_type)
        keypair.fingerprint = fingerprint
        keypair.public_key = public_key
        keypair.create()
        if CONF.quota.recheck_quota:
            try:
                objects.Quotas.check_deltas(context, {'key_pairs': 0}, user_id)
            except exception.OverQuota:
                keypair.destroy()
                raise exception.KeypairLimitExceeded()
        compute_utils.notify_about_keypair_action(context=context, keypair=keypair, action=fields_obj.NotificationAction.CREATE, phase=fields_obj.NotificationPhase.END)
        self._notify(context, 'create.end', key_name)
        return (keypair, private_key)

    def _generate_fingerprint(self, public_key, key_type):
        if key_type == keypair_obj.KEYPAIR_TYPE_SSH:
            return crypto.generate_fingerprint(public_key)
        elif key_type == keypair_obj.KEYPAIR_TYPE_X509:
            return crypto.generate_x509_fingerprint(public_key)

    def _generate_key_pair(self, user_id, key_type):
        if key_type == keypair_obj.KEYPAIR_TYPE_SSH:
            return crypto.generate_key_pair()
        elif key_type == keypair_obj.KEYPAIR_TYPE_X509:
            return crypto.generate_winrm_x509_cert(user_id)

    @wrap_exception()
    def delete_key_pair(self, context, user_id, key_name):
        """Delete a keypair by name."""
        self._notify(context, 'delete.start', key_name)
        keypair = self.get_key_pair(context, user_id, key_name)
        compute_utils.notify_about_keypair_action(context=context, keypair=keypair, action=fields_obj.NotificationAction.DELETE, phase=fields_obj.NotificationPhase.START)
        objects.KeyPair.destroy_by_name(context, user_id, key_name)
        compute_utils.notify_about_keypair_action(context=context, keypair=keypair, action=fields_obj.NotificationAction.DELETE, phase=fields_obj.NotificationPhase.END)
        self._notify(context, 'delete.end', key_name)

    def get_key_pairs(self, context, user_id, limit=None, marker=None):
        """List key pairs."""
        return objects.KeyPairList.get_by_user(context, user_id, limit=limit, marker=marker)

    def get_key_pair(self, context, user_id, key_name):
        """Get a keypair by name."""
        return objects.KeyPair.get_by_name(context, user_id, key_name)