from bees import profiler as p
'Handles database requests from other nova services.'
import collections
import contextlib
import copy
import eventlet
import functools
import sys
from oslo_config import cfg
from oslo_db import exception as db_exc
from oslo_log import log as logging
import oslo_messaging as messaging
from oslo_serialization import jsonutils
from oslo_utils import excutils
from oslo_utils import timeutils
from oslo_utils import versionutils
from nova.accelerator import cyborg
from nova import availability_zones
from nova.compute import instance_actions
from nova.compute import rpcapi as compute_rpcapi
from nova.compute import task_states
from nova.compute import utils as compute_utils
from nova.compute.utils import wrap_instance_event
from nova.compute import vm_states
from nova.conductor.tasks import cross_cell_migrate
from nova.conductor.tasks import live_migrate
from nova.conductor.tasks import migrate
from nova import context as nova_context
from nova.db import base
from nova import exception
from nova.i18n import _
from nova.image import glance
from nova import manager
from nova.network import neutron
from nova import notifications
from nova import objects
from nova.objects import base as nova_object
from nova.objects import fields
from nova import profiler
from nova import rpc
from nova.scheduler.client import query
from nova.scheduler.client import report
from nova.scheduler import utils as scheduler_utils
from nova import servicegroup
from nova import utils
from nova.volume import cinder
LOG = logging.getLogger(__name__)
CONF = cfg.CONF

@p.trace('targets_cell')
def targets_cell(fn):
    """Wrap a method and automatically target the instance's cell.

    This decorates a method with signature func(self, context, instance, ...)
    and automatically targets the context with the instance's cell
    mapping. It does this by looking up the InstanceMapping.
    """

    @functools.wraps(fn)
    def wrapper(self, context, *args, **kwargs):
        instance = kwargs.get('instance') or args[0]
        try:
            im = objects.InstanceMapping.get_by_instance_uuid(context, instance.uuid)
        except exception.InstanceMappingNotFound:
            LOG.error('InstanceMapping not found, unable to target cell', instance=instance)
        except db_exc.CantStartEngineError:
            with excutils.save_and_reraise_exception() as err_ctxt:
                if CONF.api_database.connection is None:
                    err_ctxt.reraise = False
        else:
            LOG.debug('Targeting cell %(cell)s for conductor method %(meth)s', {'cell': im.cell_mapping.identity, 'meth': fn.__name__})
            nova_context.set_target_cell(context, im.cell_mapping)
        return fn(self, context, *args, **kwargs)
    return wrapper

@p.trace_cls('ConductorManager')
class ConductorManager(manager.Manager):
    """Mission: Conduct things.

    The methods in the base API for nova-conductor are various proxy operations
    performed on behalf of the nova-compute service running on compute nodes.
    Compute nodes are not allowed to directly access the database, so this set
    of methods allows them to get specific work done without locally accessing
    the database.

    The nova-conductor service also exposes an API in the 'compute_task'
    namespace.  See the ComputeTaskManager class for details.
    """
    target = messaging.Target(version='3.0')

    def __init__(self, *args, **kwargs):
        super(ConductorManager, self).__init__(*args, service_name='conductor', **kwargs)
        self.compute_task_mgr = ComputeTaskManager()
        self.additional_endpoints.append(self.compute_task_mgr)

    def provider_fw_rule_get_all(self, context):
        return []

    def _object_dispatch(self, target, method, args, kwargs):
        """Dispatch a call to an object method.

        This ensures that object methods get called and any exception
        that is raised gets wrapped in an ExpectedException for forwarding
        back to the caller (without spamming the conductor logs).
        """
        try:
            return getattr(target, method)(*args, **kwargs)
        except Exception:
            raise messaging.ExpectedException()

    def object_class_action_versions(self, context, objname, objmethod, object_versions, args, kwargs):
        objclass = nova_object.NovaObject.obj_class_from_name(objname, object_versions[objname])
        args = tuple([context] + list(args))
        result = self._object_dispatch(objclass, objmethod, args, kwargs)
        if isinstance(result, nova_object.NovaObject):
            target_version = object_versions[objname]
            requested_version = versionutils.convert_version_to_tuple(target_version)
            actual_version = versionutils.convert_version_to_tuple(result.VERSION)
            do_backport = requested_version < actual_version
            other_major_version = requested_version[0] != actual_version[0]
            if do_backport or other_major_version:
                result = result.obj_to_primitive(target_version=target_version, version_manifest=object_versions)
        return result

    def object_action(self, context, objinst, objmethod, args, kwargs):
        """Perform an action on an object."""
        oldobj = objinst.obj_clone()
        result = self._object_dispatch(objinst, objmethod, args, kwargs)
        updates = dict()
        for (name, field) in objinst.fields.items():
            if not objinst.obj_attr_is_set(name):
                continue
            if not oldobj.obj_attr_is_set(name) or getattr(oldobj, name) != getattr(objinst, name):
                updates[name] = field.to_primitive(objinst, name, getattr(objinst, name))
        updates['obj_what_changed'] = objinst.obj_what_changed()
        return (updates, result)

    def object_backport_versions(self, context, objinst, object_versions):
        target = object_versions[objinst.obj_name()]
        LOG.debug('Backporting %(obj)s to %(ver)s with versions %(manifest)s', {'obj': objinst.obj_name(), 'ver': target, 'manifest': ','.join(['%s=%s' % (name, ver) for (name, ver) in object_versions.items()])})
        return objinst.obj_to_primitive(target_version=target, version_manifest=object_versions)

    def reset(self):
        objects.Service.clear_min_version_cache()

@p.trace('try_target_cell')
@contextlib.contextmanager
def try_target_cell(context, cell):
    """If cell is not None call func with context.target_cell.

    This is a method to help during the transition period. Currently
    various mappings may not exist if a deployment has not migrated to
    cellsv2. If there is no mapping call the func as normal, otherwise
    call it in a target_cell context.
    """
    if cell:
        with nova_context.target_cell(context, cell) as cell_context:
            yield cell_context
    else:
        yield context

@p.trace('obj_target_cell')
@contextlib.contextmanager
def obj_target_cell(obj, cell):
    """Run with object's context set to a specific cell"""
    with try_target_cell(obj._context, cell) as target:
        with obj.obj_alternate_context(target):
            yield target

@p.trace_cls('ComputeTaskManager')
@profiler.trace_cls('rpc')
class ComputeTaskManager(base.Base):
    """Namespace for compute methods.

    This class presents an rpc API for nova-conductor under the 'compute_task'
    namespace.  The methods here are compute operations that are invoked
    by the API service.  These methods see the operation to completion, which
    may involve coordinating activities on multiple compute nodes.
    """
    target = messaging.Target(namespace='compute_task', version='1.23')

    def __init__(self):
        super(ComputeTaskManager, self).__init__()
        self.compute_rpcapi = compute_rpcapi.ComputeAPI()
        self.volume_api = cinder.API()
        self.image_api = glance.API()
        self.network_api = neutron.API()
        self.servicegroup_api = servicegroup.API()
        self.query_client = query.SchedulerQueryClient()
        self.report_client = report.SchedulerReportClient()
        self.notifier = rpc.get_notifier('compute')
        self.host = CONF.host

    def reset(self):
        LOG.info('Reloading compute RPC API')
        compute_rpcapi.LAST_VERSION = None
        self.compute_rpcapi = compute_rpcapi.ComputeAPI()

    @messaging.expected_exceptions(exception.NoValidHost, exception.ComputeServiceUnavailable, exception.ComputeHostNotFound, exception.InvalidHypervisorType, exception.InvalidCPUInfo, exception.UnableToMigrateToSelf, exception.DestinationHypervisorTooOld, exception.InvalidLocalStorage, exception.InvalidSharedStorage, exception.HypervisorUnavailable, exception.InstanceInvalidState, exception.MigrationPreCheckError, exception.UnsupportedPolicyException)
    @targets_cell
    @wrap_instance_event(prefix='conductor')
    def migrate_server(self, context, instance, scheduler_hint, live, rebuild, flavor, block_migration, disk_over_commit, reservations=None, clean_shutdown=True, request_spec=None, host_list=None):
        if instance and (not isinstance(instance, nova_object.NovaObject)):
            attrs = ['metadata', 'system_metadata', 'info_cache', 'security_groups']
            instance = objects.Instance._from_db_object(context, objects.Instance(), instance, expected_attrs=attrs)
        if flavor and (not isinstance(flavor, objects.Flavor)):
            flavor = objects.Flavor.get_by_id(context, flavor['id'])
        if live and (not rebuild) and (not flavor):
            self._live_migrate(context, instance, scheduler_hint, block_migration, disk_over_commit, request_spec)
        elif not live and (not rebuild) and flavor:
            instance_uuid = instance.uuid
            with compute_utils.EventReporter(context, 'cold_migrate', self.host, instance_uuid):
                self._cold_migrate(context, instance, flavor, scheduler_hint['filter_properties'], clean_shutdown, request_spec, host_list)
        else:
            raise NotImplementedError()

    @staticmethod
    def _get_request_spec_for_cold_migrate(context, instance, flavor, filter_properties, request_spec):
        if not request_spec:
            image_meta = utils.get_image_from_system_metadata(instance.system_metadata)
            request_spec = objects.RequestSpec.from_components(context, instance.uuid, image_meta, flavor, instance.numa_topology, instance.pci_requests, filter_properties, None, instance.availability_zone, project_id=instance.project_id, user_id=instance.user_id)
        elif not isinstance(request_spec, objects.RequestSpec):
            request_spec = objects.RequestSpec.from_primitives(context, request_spec, filter_properties)
        else:
            request_spec.flavor = flavor
        return request_spec

    def _cold_migrate(self, context, instance, flavor, filter_properties, clean_shutdown, request_spec, host_list):
        request_spec = self._get_request_spec_for_cold_migrate(context, instance, flavor, filter_properties, request_spec)
        task = self._build_cold_migrate_task(context, instance, flavor, request_spec, clean_shutdown, host_list)
        try:
            task.execute()
        except exception.NoValidHost as ex:
            vm_state = instance.vm_state
            if not vm_state:
                vm_state = vm_states.ACTIVE
            updates = {'vm_state': vm_state, 'task_state': None}
            self._set_vm_state_and_notify(context, instance.uuid, 'migrate_server', updates, ex, request_spec)
            if flavor.id == instance.instance_type_id:
                msg = _('No valid host found for cold migrate')
            else:
                msg = _('No valid host found for resize')
            raise exception.NoValidHost(reason=msg)
        except exception.UnsupportedPolicyException as ex:
            with excutils.save_and_reraise_exception():
                vm_state = instance.vm_state
                if not vm_state:
                    vm_state = vm_states.ACTIVE
                updates = {'vm_state': vm_state, 'task_state': None}
                self._set_vm_state_and_notify(context, instance.uuid, 'migrate_server', updates, ex, request_spec)
        except Exception as ex:
            with excutils.save_and_reraise_exception():
                try:
                    instance.refresh()
                    updates = {'vm_state': instance.vm_state, 'task_state': None}
                    self._set_vm_state_and_notify(context, instance.uuid, 'migrate_server', updates, ex, request_spec)
                except exception.InstanceNotFound:
                    LOG.info('During %s the instance was deleted.', 'resize' if instance.instance_type_id != flavor.id else 'cold migrate', instance=instance)
        if request_spec.obj_what_changed():
            request_spec.save()

    def _set_vm_state_and_notify(self, context, instance_uuid, method, updates, ex, request_spec):
        scheduler_utils.set_vm_state_and_notify(context, instance_uuid, 'compute_task', method, updates, ex, request_spec)

    def _cleanup_allocated_networks(self, context, instance, requested_networks):
        try:
            if not (requested_networks and requested_networks.no_allocate):
                self.network_api.deallocate_for_instance(context, instance, requested_networks=requested_networks)
        except Exception:
            LOG.exception('Failed to deallocate networks', instance=instance)
            return
        instance.system_metadata['network_allocated'] = 'False'
        try:
            instance.save()
        except exception.InstanceNotFound:
            pass

    @targets_cell
    @wrap_instance_event(prefix='conductor')
    def live_migrate_instance(self, context, instance, scheduler_hint, block_migration, disk_over_commit, request_spec):
        self._live_migrate(context, instance, scheduler_hint, block_migration, disk_over_commit, request_spec)

    def _live_migrate(self, context, instance, scheduler_hint, block_migration, disk_over_commit, request_spec):
        destination = scheduler_hint.get('host')

        def _set_vm_state(context, instance, ex, vm_state=None, task_state=None):
            request_spec = {'instance_properties': {'uuid': instance.uuid}}
            scheduler_utils.set_vm_state_and_notify(context, instance.uuid, 'compute_task', 'migrate_server', dict(vm_state=vm_state, task_state=task_state, expected_task_state=task_states.MIGRATING), ex, request_spec)
        migration = objects.Migration(context=context.elevated())
        migration.dest_compute = destination
        migration.status = 'accepted'
        migration.instance_uuid = instance.uuid
        migration.source_compute = instance.host
        migration.migration_type = fields.MigrationType.LIVE_MIGRATION
        if instance.obj_attr_is_set('flavor'):
            migration.old_instance_type_id = instance.flavor.id
            migration.new_instance_type_id = instance.flavor.id
        else:
            migration.old_instance_type_id = instance.instance_type_id
            migration.new_instance_type_id = instance.instance_type_id
        migration.create()
        task = self._build_live_migrate_task(context, instance, destination, block_migration, disk_over_commit, migration, request_spec)
        try:
            task.execute()
        except (exception.NoValidHost, exception.ComputeHostNotFound, exception.ComputeServiceUnavailable, exception.InvalidHypervisorType, exception.InvalidCPUInfo, exception.UnableToMigrateToSelf, exception.DestinationHypervisorTooOld, exception.InvalidLocalStorage, exception.InvalidSharedStorage, exception.HypervisorUnavailable, exception.InstanceInvalidState, exception.MigrationPreCheckError, exception.MigrationSchedulerRPCError) as ex:
            with excutils.save_and_reraise_exception():
                _set_vm_state(context, instance, ex, instance.vm_state)
                migration.status = 'error'
                migration.save()
        except Exception as ex:
            LOG.error('Migration of instance %(instance_id)s to host %(dest)s unexpectedly failed.', {'instance_id': instance.uuid, 'dest': destination}, exc_info=True)
            _set_vm_state(context, instance, ex, vm_states.ERROR, task_state=None)
            migration.status = 'error'
            migration.save()
            raise exception.MigrationError(reason=str(ex))

    def _build_live_migrate_task(self, context, instance, destination, block_migration, disk_over_commit, migration, request_spec=None):
        return live_migrate.LiveMigrationTask(context, instance, destination, block_migration, disk_over_commit, migration, self.compute_rpcapi, self.servicegroup_api, self.query_client, self.report_client, request_spec)

    def _build_cold_migrate_task(self, context, instance, flavor, request_spec, clean_shutdown, host_list):
        return migrate.MigrationTask(context, instance, flavor, request_spec, clean_shutdown, self.compute_rpcapi, self.query_client, self.report_client, host_list, self.network_api)

    def _destroy_build_request(self, context, instance):
        build_request = objects.BuildRequest.get_by_instance_uuid(context, instance.uuid)
        build_request.destroy()

    def _populate_instance_mapping(self, context, instance, host):
        try:
            inst_mapping = objects.InstanceMapping.get_by_instance_uuid(context, instance.uuid)
        except exception.InstanceMappingNotFound:
            LOG.debug('Instance was not mapped to a cell, likely due to an older nova-api service running.', instance=instance)
            return None
        else:
            try:
                host_mapping = objects.HostMapping.get_by_host(context, host.service_host)
            except exception.HostMappingNotFound:
                inst_mapping.destroy()
                return None
            else:
                inst_mapping.cell_mapping = host_mapping.cell_mapping
                inst_mapping.save()
        return inst_mapping

    def _validate_existing_attachment_ids(self, context, instance, bdms):
        """Ensure any attachment ids referenced by the bdms exist.

        New attachments will only be created if the attachment ids referenced
        by the bdms no longer exist. This can happen when an instance is
        rescheduled after a failure to spawn as cleanup code on the previous
        host will delete attachments before rescheduling.
        """
        for bdm in bdms:
            if bdm.is_volume and bdm.attachment_id:
                try:
                    self.volume_api.attachment_get(context, bdm.attachment_id)
                except exception.VolumeAttachmentNotFound:
                    attachment = self.volume_api.attachment_create(context, bdm.volume_id, instance.uuid)
                    bdm.attachment_id = attachment['id']
                    bdm.save()

    def _cleanup_when_reschedule_fails(self, context, instance, exception, legacy_request_spec, requested_networks):
        """Set the instance state and clean up.

        It is only used in case build_instance fails while rescheduling the
        instance
        """
        updates = {'vm_state': vm_states.ERROR, 'task_state': None}
        self._set_vm_state_and_notify(context, instance.uuid, 'build_instances', updates, exception, legacy_request_spec)
        self._cleanup_allocated_networks(context, instance, requested_networks)
        compute_utils.delete_arqs_if_needed(context, instance)

    def build_instances(self, context, instances, image, filter_properties, admin_password, injected_files, requested_networks, security_groups, block_device_mapping=None, legacy_bdm=True, request_spec=None, host_lists=None):
        if requested_networks and (not isinstance(requested_networks, objects.NetworkRequestList)):
            requested_networks = objects.NetworkRequestList.from_tuples(requested_networks)
        flavor = filter_properties.get('instance_type')
        if flavor and (not isinstance(flavor, objects.Flavor)):
            flavor = objects.Flavor.get_by_id(context, flavor['id'])
            filter_properties = dict(filter_properties, instance_type=flavor)
        if request_spec is None:
            legacy_request_spec = scheduler_utils.build_request_spec(image, instances)
        else:
            legacy_request_spec = request_spec.to_legacy_request_spec_dict()
        is_reschedule = host_lists is not None
        try:
            scheduler_utils.populate_retry(filter_properties, instances[0].uuid)
            instance_uuids = [instance.uuid for instance in instances]
            spec_obj = objects.RequestSpec.from_primitives(context, legacy_request_spec, filter_properties)
            LOG.debug('Rescheduling: %s', is_reschedule)
            if is_reschedule:
                if not host_lists[0]:
                    msg = 'Exhausted all hosts available for retrying build failures for instance %(instance_uuid)s.' % {'instance_uuid': instances[0].uuid}
                    raise exception.MaxRetriesExceeded(reason=msg)
            else:
                host_lists = self._schedule_instances(context, spec_obj, instance_uuids, return_alternates=True)
        except Exception as exc:
            num_attempts = filter_properties.get('retry', {}).get('num_attempts', 1)
            for instance in instances:
                if num_attempts <= 1:
                    try:
                        self._destroy_build_request(context, instance)
                    except exception.BuildRequestNotFound:
                        pass
                self._cleanup_when_reschedule_fails(context, instance, exc, legacy_request_spec, requested_networks)
            return
        elevated = context.elevated()
        for (instance, host_list) in zip(instances, host_lists):
            host = host_list.pop(0)
            if is_reschedule:
                host_available = False
                while host and (not host_available):
                    if host.allocation_request:
                        alloc_req = jsonutils.loads(host.allocation_request)
                    else:
                        alloc_req = None
                    if alloc_req:
                        try:
                            host_available = scheduler_utils.claim_resources(elevated, self.report_client, spec_obj, instance.uuid, alloc_req, host.allocation_request_version)
                            if request_spec and host_available:
                                scheduler_utils.fill_provider_mapping(request_spec, host)
                        except Exception as exc:
                            self._cleanup_when_reschedule_fails(context, instance, exc, legacy_request_spec, requested_networks)
                            return
                    else:
                        host_available = True
                    if not host_available:
                        host = host_list.pop(0) if host_list else None
                if not host_available:
                    msg = 'Exhausted all hosts available for retrying build failures for instance %(instance_uuid)s.' % {'instance_uuid': instance.uuid}
                    exc = exception.MaxRetriesExceeded(reason=msg)
                    self._cleanup_when_reschedule_fails(context, instance, exc, legacy_request_spec, requested_networks)
                    return
            if 'availability_zone' in host:
                instance.availability_zone = host.availability_zone
            else:
                try:
                    instance.availability_zone = availability_zones.get_host_availability_zone(context, host.service_host)
                except Exception as exc:
                    self._cleanup_when_reschedule_fails(context, instance, exc, legacy_request_spec, requested_networks)
                    continue
            try:
                instance.save()
            except (exception.InstanceNotFound, exception.InstanceInfoCacheNotFound):
                LOG.debug('Instance deleted during build', instance=instance)
                continue
            local_filter_props = copy.deepcopy(filter_properties)
            scheduler_utils.populate_filter_properties(local_filter_props, host)
            local_reqspec = objects.RequestSpec.from_primitives(context, legacy_request_spec, local_filter_props)
            if request_spec:
                local_reqspec.requested_resources = request_spec.requested_resources
            bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(context, instance.uuid)
            num_attempts = local_filter_props.get('retry', {}).get('num_attempts', 1)
            if num_attempts <= 1:
                inst_mapping = self._populate_instance_mapping(context, instance, host)
                try:
                    self._destroy_build_request(context, instance)
                except exception.BuildRequestNotFound:
                    if inst_mapping:
                        inst_mapping.destroy()
                    return
            else:
                self._validate_existing_attachment_ids(context, instance, bdms)
            alts = [(alt.service_host, alt.nodename) for alt in host_list]
            LOG.debug('Selected host: %s; Selected node: %s; Alternates: %s', host.service_host, host.nodename, alts, instance=instance)
            try:
                accel_uuids = self._create_and_bind_arq_for_instance(context, instance, host.nodename, local_reqspec)
            except Exception as exc:
                LOG.exception('Failed to reschedule. Reason: %s', exc)
                self._cleanup_when_reschedule_fails(context, instance, exc, legacy_request_spec, requested_networks)
                continue
            self.compute_rpcapi.build_and_run_instance(context, instance=instance, host=host.service_host, image=image, request_spec=local_reqspec, filter_properties=local_filter_props, admin_password=admin_password, injected_files=injected_files, requested_networks=requested_networks, security_groups=security_groups, block_device_mapping=bdms, node=host.nodename, limits=host.limits, host_list=host_list, accel_uuids=accel_uuids)

    def _create_and_bind_arq_for_instance(self, context, instance, hostname, request_spec):
        try:
            resource_provider_mapping = request_spec.get_request_group_mapping()
            return self._create_and_bind_arqs(context, instance.uuid, instance.flavor.extra_specs, hostname, resource_provider_mapping)
        except exception.AcceleratorRequestBindingFailed as exc:
            cyclient = cyborg.get_client(context)
            cyclient.delete_arqs_by_uuid(exc.arqs)
            raise

    def _schedule_instances(self, context, request_spec, instance_uuids=None, return_alternates=False):
        scheduler_utils.setup_instance_group(context, request_spec)
        with timeutils.StopWatch() as timer:
            host_lists = self.query_client.select_destinations(context, request_spec, instance_uuids, return_objects=True, return_alternates=return_alternates)
        LOG.debug('Took %0.2f seconds to select destinations for %s instance(s).', timer.elapsed(), len(instance_uuids))
        return host_lists

    @staticmethod
    def _restrict_request_spec_to_cell(context, instance, request_spec):
        """Sets RequestSpec.requested_destination.cell for the move operation

        Move operations, e.g. evacuate and unshelve, must be restricted to the
        cell in which the instance already exists, so this method is used to
        target the RequestSpec, which is sent to the scheduler via the
        _schedule_instances method, to the instance's current cell.

        :param context: nova auth RequestContext
        """
        instance_mapping = objects.InstanceMapping.get_by_instance_uuid(context, instance.uuid)
        LOG.debug('Requesting cell %(cell)s during scheduling', {'cell': instance_mapping.cell_mapping.identity}, instance=instance)
        if 'requested_destination' in request_spec and request_spec.requested_destination:
            request_spec.requested_destination.cell = instance_mapping.cell_mapping
        else:
            request_spec.requested_destination = objects.Destination(cell=instance_mapping.cell_mapping)

    @targets_cell
    def unshelve_instance(self, context, instance, request_spec=None):
        sys_meta = instance.system_metadata

        def safe_image_show(ctx, image_id):
            if image_id:
                return self.image_api.get(ctx, image_id, show_deleted=False)
            else:
                raise exception.ImageNotFound(image_id='')
        if instance.vm_state == vm_states.SHELVED:
            instance.task_state = task_states.POWERING_ON
            instance.save(expected_task_state=task_states.UNSHELVING)
            self.compute_rpcapi.start_instance(context, instance)
        elif instance.vm_state == vm_states.SHELVED_OFFLOADED:
            image = None
            image_id = sys_meta.get('shelved_image_id')
            if image_id:
                with compute_utils.EventReporter(context, 'get_image_info', self.host, instance.uuid):
                    try:
                        image = safe_image_show(context, image_id)
                    except exception.ImageNotFound as error:
                        instance.vm_state = vm_states.ERROR
                        instance.save()
                        reason = _('Unshelve attempted but the image %s cannot be found.') % image_id
                        LOG.error(reason, instance=instance)
                        compute_utils.add_instance_fault_from_exc(context, instance, error, sys.exc_info(), fault_message=reason)
                        raise exception.UnshelveException(instance_id=instance.uuid, reason=reason)
            try:
                with compute_utils.EventReporter(context, 'schedule_instances', self.host, instance.uuid):
                    request_spec.reset_forced_destinations()
                    filter_properties = request_spec.to_legacy_filter_properties_dict()
                    external_resources = self.network_api.get_requested_resource_for_instance(context, instance.uuid)
                    extra_specs = request_spec.flavor.extra_specs
                    device_profile = extra_specs.get('accel:device_profile')
                    external_resources.extend(cyborg.get_device_profile_request_groups(context, device_profile) if device_profile else [])
                    request_spec.requested_resources = external_resources
                    self._restrict_request_spec_to_cell(context, instance, request_spec)
                    request_spec.ensure_project_and_user_id(instance)
                    request_spec.ensure_network_information(instance)
                    compute_utils.heal_reqspec_is_bfv(context, request_spec, instance)
                    host_lists = self._schedule_instances(context, request_spec, [instance.uuid], return_alternates=False)
                    host_list = host_lists[0]
                    selection = host_list[0]
                    scheduler_utils.populate_filter_properties(filter_properties, selection)
                    (host, node) = (selection.service_host, selection.nodename)
                    instance.availability_zone = availability_zones.get_host_availability_zone(context, host)
                    scheduler_utils.fill_provider_mapping(request_spec, selection)
                    accel_uuids = self._create_and_bind_arq_for_instance(context, instance, node, request_spec)
                    self.compute_rpcapi.unshelve_instance(context, instance, host, request_spec, image=image, filter_properties=filter_properties, node=node, accel_uuids=accel_uuids)
            except (exception.NoValidHost, exception.UnsupportedPolicyException):
                instance.task_state = None
                instance.save()
                LOG.warning('No valid host found for unshelve instance', instance=instance)
                return
            except Exception as exc:
                if isinstance(exc, exception.AcceleratorRequestBindingFailed):
                    cyclient = cyborg.get_client(context)
                    cyclient.delete_arqs_by_uuid(exc.arqs)
                LOG.exception('Failed to unshelve. Reason: %s', exc)
                with excutils.save_and_reraise_exception():
                    instance.task_state = None
                    instance.save()
                    LOG.error('Unshelve attempted but an error has occurred', instance=instance)
        else:
            LOG.error('Unshelve attempted but vm_state not SHELVED or SHELVED_OFFLOADED', instance=instance)
            instance.vm_state = vm_states.ERROR
            instance.save()
            return

    def _allocate_for_evacuate_dest_host(self, context, instance, host, request_spec=None):
        source_node = None
        try:
            source_node = objects.ComputeNode.get_by_host_and_nodename(context, instance.host, instance.node)
            dest_node = objects.ComputeNode.get_first_node_by_host_for_old_compat(context, host, use_slave=True)
        except exception.ComputeHostNotFound as ex:
            with excutils.save_and_reraise_exception():
                self._set_vm_state_and_notify(context, instance.uuid, 'rebuild_server', {'vm_state': instance.vm_state, 'task_state': None}, ex, request_spec)
                if source_node:
                    LOG.warning('Specified host %s for evacuate was not found.', host, instance=instance)
                else:
                    LOG.warning('Source host %s and node %s for evacuate was not found.', instance.host, instance.node, instance=instance)
        try:
            scheduler_utils.claim_resources_on_destination(context, self.report_client, instance, source_node, dest_node)
        except exception.NoValidHost as ex:
            with excutils.save_and_reraise_exception():
                self._set_vm_state_and_notify(context, instance.uuid, 'rebuild_server', {'vm_state': instance.vm_state, 'task_state': None}, ex, request_spec)
                LOG.warning('Specified host %s for evacuate is invalid.', host, instance=instance)

    @targets_cell
    def rebuild_instance(self, context, instance, orig_image_ref, image_ref, injected_files, new_pass, orig_sys_metadata, bdms, recreate, on_shared_storage, preserve_ephemeral=False, host=None, request_spec=None):
        evacuate = recreate
        with compute_utils.EventReporter(context, 'rebuild_server', self.host, instance.uuid):
            node = limits = None
            try:
                migration = objects.Migration.get_by_instance_and_status(context, instance.uuid, 'accepted')
            except exception.MigrationNotFoundByStatus:
                LOG.debug('No migration record for the rebuild/evacuate request.', instance=instance)
                migration = None
            if host:
                if host != instance.host:
                    try:
                        self._allocate_for_evacuate_dest_host(context, instance, host, request_spec)
                    except exception.AllocationUpdateFailed as ex:
                        with excutils.save_and_reraise_exception():
                            if migration:
                                migration.status = 'error'
                                migration.save()
                            self._set_vm_state_and_notify(context, instance.uuid, 'rebuild_server', {'vm_state': vm_states.ERROR, 'task_state': None}, ex, request_spec)
                            LOG.warning('Rebuild failed: %s', str(ex), instance=instance)
                    except exception.NoValidHost:
                        with excutils.save_and_reraise_exception():
                            if migration:
                                migration.status = 'error'
                                migration.save()
            else:
                if evacuate:
                    request_spec.ignore_hosts = [instance.host]
                    request_spec.reset_forced_destinations()
                    external_resources = []
                    external_resources += self.network_api.get_requested_resource_for_instance(context, instance.uuid)
                    extra_specs = request_spec.flavor.extra_specs
                    device_profile = extra_specs.get('accel:device_profile')
                    external_resources.extend(cyborg.get_device_profile_request_groups(context, device_profile) if device_profile else [])
                    request_spec.requested_resources = external_resources
                try:
                    if not evacuate and orig_image_ref != image_ref:
                        self._validate_image_traits_for_rebuild(context, instance, image_ref)
                    self._restrict_request_spec_to_cell(context, instance, request_spec)
                    request_spec.ensure_project_and_user_id(instance)
                    request_spec.ensure_network_information(instance)
                    compute_utils.heal_reqspec_is_bfv(context, request_spec, instance)
                    host_lists = self._schedule_instances(context, request_spec, [instance.uuid], return_alternates=False)
                    host_list = host_lists[0]
                    selection = host_list[0]
                    (host, node, limits) = (selection.service_host, selection.nodename, selection.limits)
                    if recreate:
                        scheduler_utils.fill_provider_mapping(request_spec, selection)
                except (exception.NoValidHost, exception.UnsupportedPolicyException, exception.AllocationUpdateFailed, NotImplementedError, ValueError) as ex:
                    if migration:
                        migration.status = 'error'
                        migration.save()
                    if orig_image_ref and orig_image_ref != image_ref:
                        instance.image_ref = orig_image_ref
                        instance.save()
                    with excutils.save_and_reraise_exception():
                        self._set_vm_state_and_notify(context, instance.uuid, 'rebuild_server', {'vm_state': vm_states.ERROR, 'task_state': None}, ex, request_spec)
                        LOG.warning('Rebuild failed: %s', str(ex), instance=instance)
            compute_utils.notify_about_instance_usage(self.notifier, context, instance, 'rebuild.scheduled')
            compute_utils.notify_about_instance_rebuild(context, instance, host, action=fields.NotificationAction.REBUILD_SCHEDULED, source=fields.NotificationSource.CONDUCTOR)
            instance.availability_zone = availability_zones.get_host_availability_zone(context, host)
            accel_uuids = []
            try:
                if instance.flavor.extra_specs.get('accel:device_profile'):
                    cyclient = cyborg.get_client(context)
                    if evacuate:
                        cyclient.delete_arqs_for_instance(instance.uuid)
                        accel_uuids = self._create_and_bind_arq_for_instance(context, instance, node, request_spec)
                    else:
                        accel_uuids = cyclient.get_arq_uuids_for_instance(instance)
            except Exception as exc:
                if isinstance(exc, exception.AcceleratorRequestBindingFailed):
                    cyclient = cyborg.get_client(context)
                    cyclient.delete_arqs_by_uuid(exc.arqs)
                LOG.exception('Failed to rebuild. Reason: %s', exc)
                raise exc
            self.compute_rpcapi.rebuild_instance(context, instance=instance, new_pass=new_pass, injected_files=injected_files, image_ref=image_ref, orig_image_ref=orig_image_ref, orig_sys_metadata=orig_sys_metadata, bdms=bdms, recreate=evacuate, on_shared_storage=on_shared_storage, preserve_ephemeral=preserve_ephemeral, migration=migration, host=host, node=node, limits=limits, request_spec=request_spec, accel_uuids=accel_uuids)

    def _validate_image_traits_for_rebuild(self, context, instance, image_ref):
        """Validates that the traits specified in the image can be satisfied
        by the providers of the current allocations for the instance during
        rebuild of the instance. If the traits cannot be
        satisfied, fails the action by raising a NoValidHost exception.

        :raises: NoValidHost exception in case the traits on the providers
                 of the allocated resources for the instance do not match
                 the required traits on the image.
        """
        image_meta = objects.ImageMeta.from_image_ref(context, self.image_api, image_ref)
        if 'properties' not in image_meta or 'traits_required' not in image_meta.properties or (not image_meta.properties.traits_required):
            return
        image_traits = set(image_meta.properties.traits_required)
        extra_specs = instance.flavor.extra_specs
        forbidden_flavor_traits = set()
        for (key, val) in extra_specs.items():
            if key.startswith('trait'):
                (prefix, parsed_key) = key.split(':', 1)
                if val == 'forbidden':
                    forbidden_flavor_traits.add(parsed_key)
        forbidden_traits = image_traits & forbidden_flavor_traits
        if forbidden_traits:
            raise exception.NoValidHost(reason=_('Image traits are part of forbidden traits in flavor associated with the server. Either specify a different image during rebuild or create a new server with the specified image and a compatible flavor.'))
        allocations = self.report_client.get_allocations_for_consumer(context, instance.uuid)
        instance_rp_uuids = list(allocations)
        compute_node = objects.ComputeNode.get_by_host_and_nodename(context, instance.host, instance.node)
        instance_rp_tree = self.report_client.get_provider_tree_and_ensure_root(context, compute_node.uuid)
        traits_in_instance_rps = set()
        for rp_uuid in instance_rp_uuids:
            traits_in_instance_rps.update(instance_rp_tree.data(rp_uuid).traits)
        missing_traits = image_traits - traits_in_instance_rps
        if missing_traits:
            raise exception.NoValidHost(reason=_('Image traits cannot be satisfied by the current resource providers. Either specify a different image during rebuild or create a new server with the specified image.'))

    @staticmethod
    def _volume_size(instance_type, bdm):
        size = bdm.get('volume_size')
        if size is None and bdm.get('source_type') == 'blank' and (bdm.get('destination_type') == 'local'):
            if bdm.get('guest_format') == 'swap':
                size = instance_type.get('swap', 0)
            else:
                size = instance_type.get('ephemeral_gb', 0)
        return size

    def _create_block_device_mapping(self, cell, instance_type, instance_uuid, block_device_mapping):
        """Create the BlockDeviceMapping objects in the db.

        This method makes a copy of the list in order to avoid using the same
        id field in case this is called for multiple instances.
        """
        LOG.debug('block_device_mapping %s', list(block_device_mapping), instance_uuid=instance_uuid)
        instance_block_device_mapping = copy.deepcopy(block_device_mapping)
        for bdm in instance_block_device_mapping:
            bdm.volume_size = self._volume_size(instance_type, bdm)
            bdm.instance_uuid = instance_uuid
            with obj_target_cell(bdm, cell):
                bdm.update_or_create()
        return instance_block_device_mapping

    def _create_tags(self, context, instance_uuid, tags):
        """Create the Tags objects in the db."""
        if tags:
            tag_list = [tag.tag for tag in tags]
            instance_tags = objects.TagList.create(context, instance_uuid, tag_list)
            return instance_tags
        else:
            return tags

    def _create_instance_action_for_cell0(self, context, instance, exc):
        """Create a failed "create" instance action for the instance in cell0.

        :param context: nova auth RequestContext targeted at cell0
        :param instance: Instance object being buried in cell0
        :param exc: Exception that occurred which resulted in burial
        """
        objects.InstanceAction.action_start(context, instance.uuid, instance_actions.CREATE, want_result=False)
        event_name = 'conductor_schedule_and_build_instances'
        objects.InstanceActionEvent.event_start(context, instance.uuid, event_name, want_result=False, host=self.host)
        objects.InstanceActionEvent.event_finish_with_failure(context, instance.uuid, event_name, exc_val=exc, exc_tb=sys.exc_info()[2], want_result=False)

    def _bury_in_cell0(self, context, request_spec, exc, build_requests=None, instances=None, block_device_mapping=None, tags=None):
        """Ensure all provided build_requests and instances end up in cell0.

        Cell0 is the fake cell we schedule dead instances to when we can't
        schedule them somewhere real. Requests that don't yet have instances
        will get a new instance, created in cell0. Instances that have not yet
        been created will be created in cell0. All build requests are destroyed
        after we're done. Failure to delete a build request will trigger the
        instance deletion, just like the happy path in
        schedule_and_build_instances() below.
        """
        try:
            cell0 = objects.CellMapping.get_by_uuid(context, objects.CellMapping.CELL0_UUID)
        except exception.CellMappingNotFound:
            LOG.error('No cell mapping found for cell0 while trying to record scheduling failure. Setup is incomplete.')
            return
        build_requests = build_requests or []
        instances = instances or []
        instances_by_uuid = {inst.uuid: inst for inst in instances}
        for build_request in build_requests:
            if build_request.instance_uuid not in instances_by_uuid:
                instance = build_request.get_new_instance(context)
                instances_by_uuid[instance.uuid] = instance
        updates = {'vm_state': vm_states.ERROR, 'task_state': None}
        for instance in instances_by_uuid.values():
            inst_mapping = None
            try:
                inst_mapping = objects.InstanceMapping.get_by_instance_uuid(context, instance.uuid)
            except exception.InstanceMappingNotFound:
                LOG.error('While burying instance in cell0, no instance mapping was found.', instance=instance)
            if inst_mapping and inst_mapping.cell_mapping is not None:
                LOG.error('When attempting to bury instance in cell0, the instance is already mapped to cell %s. Ignoring bury in cell0 attempt.', inst_mapping.cell_mapping.identity, instance=instance)
                continue
            with obj_target_cell(instance, cell0) as cctxt:
                instance.create()
                if inst_mapping:
                    inst_mapping.cell_mapping = cell0
                    inst_mapping.save()
                self._create_instance_action_for_cell0(cctxt, instance, exc)
                if block_device_mapping:
                    self._create_block_device_mapping(cell0, instance.flavor, instance.uuid, block_device_mapping)
                self._create_tags(cctxt, instance.uuid, tags)
                self._set_vm_state_and_notify(cctxt, instance.uuid, 'build_instances', updates, exc, request_spec)
        for build_request in build_requests:
            try:
                build_request.destroy()
            except exception.BuildRequestNotFound:
                inst = instances_by_uuid[build_request.instance_uuid]
                with obj_target_cell(inst, cell0):
                    inst.destroy()

    def schedule_and_build_instances(self, context, build_requests, request_specs, image, admin_password, injected_files, requested_networks, block_device_mapping, tags=None):
        instance_uuids = [spec.instance_uuid for spec in request_specs]
        try:
            host_lists = self._schedule_instances(context, request_specs[0], instance_uuids, return_alternates=True)
        except Exception as exc:
            LOG.exception('Failed to schedule instances')
            self._bury_in_cell0(context, request_specs[0], exc, build_requests=build_requests, block_device_mapping=block_device_mapping, tags=tags)
            return
        host_mapping_cache = {}
        cell_mapping_cache = {}
        instances = []
        host_az = {}
        for (build_request, request_spec, host_list) in zip(build_requests, request_specs, host_lists):
            instance = build_request.get_new_instance(context)
            host = host_list[0]
            if host.service_host not in host_mapping_cache:
                try:
                    host_mapping = objects.HostMapping.get_by_host(context, host.service_host)
                    host_mapping_cache[host.service_host] = host_mapping
                except exception.HostMappingNotFound as exc:
                    LOG.error('No host-to-cell mapping found for selected host %(host)s. Setup is incomplete.', {'host': host.service_host})
                    self._bury_in_cell0(context, request_spec, exc, build_requests=[build_request], instances=[instance], block_device_mapping=block_device_mapping, tags=tags)
                    instances.append(None)
                    continue
            else:
                host_mapping = host_mapping_cache[host.service_host]
            cell = host_mapping.cell_mapping
            try:
                objects.BuildRequest.get_by_instance_uuid(context, instance.uuid)
            except exception.BuildRequestNotFound:
                LOG.debug('While scheduling instance, the build request was already deleted.', instance=instance)
                instances.append(None)
                try:
                    im = objects.InstanceMapping.get_by_instance_uuid(context, instance.uuid)
                    im.destroy()
                except exception.InstanceMappingNotFound:
                    pass
                self.report_client.delete_allocation_for_instance(context, instance.uuid)
                continue
            else:
                if host.service_host not in host_az:
                    host_az[host.service_host] = availability_zones.get_host_availability_zone(context, host.service_host)
                instance.availability_zone = host_az[host.service_host]
                with obj_target_cell(instance, cell):
                    instance.create()
                    instances.append(instance)
                    cell_mapping_cache[instance.uuid] = cell
        if CONF.quota.recheck_quota:
            try:
                compute_utils.check_num_instances_quota(context, instance.flavor, 0, 0, orig_num_req=len(build_requests))
            except exception.TooManyInstances as exc:
                with excutils.save_and_reraise_exception():
                    self._cleanup_build_artifacts(context, exc, instances, build_requests, request_specs, block_device_mapping, tags, cell_mapping_cache)
        zipped = zip(build_requests, request_specs, host_lists, instances)
        for (build_request, request_spec, host_list, instance) in zipped:
            if instance is None:
                continue
            cell = cell_mapping_cache[instance.uuid]
            host = host_list.pop(0)
            alts = [(alt.service_host, alt.nodename) for alt in host_list]
            LOG.debug('Selected host: %s; Selected node: %s; Alternates: %s', host.service_host, host.nodename, alts, instance=instance)
            filter_props = request_spec.to_legacy_filter_properties_dict()
            scheduler_utils.populate_retry(filter_props, instance.uuid)
            scheduler_utils.populate_filter_properties(filter_props, host)
            try:
                scheduler_utils.fill_provider_mapping(request_spec, host)
            except Exception as exc:
                with excutils.save_and_reraise_exception():
                    self._cleanup_build_artifacts(context, exc, instances, build_requests, request_specs, block_device_mapping, tags, cell_mapping_cache)
            with obj_target_cell(instance, cell) as cctxt:
                notifications.send_update_with_states(cctxt, instance, None, vm_states.BUILDING, None, None, service='conductor')
                objects.InstanceAction.action_start(cctxt, instance.uuid, instance_actions.CREATE, want_result=False)
                instance_bdms = self._create_block_device_mapping(cell, instance.flavor, instance.uuid, block_device_mapping)
                instance_tags = self._create_tags(cctxt, instance.uuid, tags)
            instance.tags = instance_tags if instance_tags else objects.TagList()
            self._map_instance_to_cell(context, instance, cell)
            if not self._delete_build_request(context, build_request, instance, cell, instance_bdms, instance_tags):
                continue
            try:
                accel_uuids = self._create_and_bind_arq_for_instance(context, instance, host.nodename, request_spec)
            except Exception as exc:
                with excutils.save_and_reraise_exception():
                    self._cleanup_build_artifacts(context, exc, instances, build_requests, request_specs, block_device_mapping, tags, cell_mapping_cache)
            legacy_secgroups = [s.identifier for s in request_spec.security_groups]
            with obj_target_cell(instance, cell) as cctxt:
                self.compute_rpcapi.build_and_run_instance(cctxt, instance=instance, image=image, request_spec=request_spec, filter_properties=filter_props, admin_password=admin_password, injected_files=injected_files, requested_networks=requested_networks, security_groups=legacy_secgroups, block_device_mapping=instance_bdms, host=host.service_host, node=host.nodename, limits=host.limits, host_list=host_list, accel_uuids=accel_uuids)

    def _create_and_bind_arqs(self, context, instance_uuid, extra_specs, hostname, resource_provider_mapping):
        """Create ARQs, determine their RPs and initiate ARQ binding.

           The binding is asynchronous; Cyborg will notify on completion.
           The notification will be handled in the compute manager.
        """
        dp_name = extra_specs.get('accel:device_profile')
        if not dp_name:
            return []
        LOG.debug('Calling Cyborg to get ARQs. dp_name=%s instance=%s', dp_name, instance_uuid)
        cyclient = cyborg.get_client(context)
        arqs = cyclient.create_arqs_and_match_resource_providers(dp_name, resource_provider_mapping)
        LOG.debug('Got ARQs with resource provider mapping %s', arqs)
        bindings = {arq['uuid']: {'hostname': hostname, 'device_rp_uuid': arq['device_rp_uuid'], 'instance_uuid': instance_uuid} for arq in arqs}
        cyclient.bind_arqs(bindings=bindings)
        return [arq['uuid'] for arq in arqs]

    @staticmethod
    def _map_instance_to_cell(context, instance, cell):
        """Update the instance mapping to point at the given cell.

        During initial scheduling once a host and cell is selected in which
        to build the instance this method is used to update the instance
        mapping to point at that cell.

        :param context: nova auth RequestContext
        :param instance: Instance object being built
        :param cell: CellMapping representing the cell in which the instance
            was created and is being built.
        :returns: InstanceMapping object that was updated.
        """
        inst_mapping = objects.InstanceMapping.get_by_instance_uuid(context, instance.uuid)
        if inst_mapping.cell_mapping is not None:
            LOG.error('During scheduling instance is already mapped to another cell: %s. This should not happen and is an indication of bigger problems. If you see this you should report it to the nova team. Overwriting the mapping to point at cell %s.', inst_mapping.cell_mapping.identity, cell.identity, instance=instance)
        inst_mapping.cell_mapping = cell
        inst_mapping.save()
        return inst_mapping

    def _cleanup_build_artifacts(self, context, exc, instances, build_requests, request_specs, block_device_mappings, tags, cell_mapping_cache):
        for (instance, build_request, request_spec) in zip(instances, build_requests, request_specs):
            if instance is None:
                continue
            updates = {'vm_state': vm_states.ERROR, 'task_state': None}
            cell = cell_mapping_cache[instance.uuid]
            with try_target_cell(context, cell) as cctxt:
                self._set_vm_state_and_notify(cctxt, instance.uuid, 'build_instances', updates, exc, request_spec)
            if block_device_mappings:
                self._create_block_device_mapping(cell, instance.flavor, instance.uuid, block_device_mappings)
            if tags:
                with nova_context.target_cell(context, cell) as cctxt:
                    self._create_tags(cctxt, instance.uuid, tags)
            inst_mapping = objects.InstanceMapping.get_by_instance_uuid(context, instance.uuid)
            inst_mapping.cell_mapping = cell
            inst_mapping.save()
            try:
                build_request.destroy()
            except exception.BuildRequestNotFound:
                pass
            try:
                request_spec.destroy()
            except exception.RequestSpecNotFound:
                pass

    def _delete_build_request(self, context, build_request, instance, cell, instance_bdms, instance_tags):
        """Delete a build request after creating the instance in the cell.

        This method handles cleaning up the instance in case the build request
        is already deleted by the time we try to delete it.

        :param context: the context of the request being handled
        :type context: nova.context.RequestContext
        :param build_request: the build request to delete
        :type build_request: nova.objects.BuildRequest
        :param instance: the instance created from the build_request
        :type instance: nova.objects.Instance
        :param cell: the cell in which the instance was created
        :type cell: nova.objects.CellMapping
        :param instance_bdms: list of block device mappings for the instance
        :type instance_bdms: nova.objects.BlockDeviceMappingList
        :param instance_tags: list of tags for the instance
        :type instance_tags: nova.objects.TagList
        :returns: True if the build request was successfully deleted, False if
            the build request was already deleted and the instance is now gone.
        """
        try:
            build_request.destroy()
        except exception.BuildRequestNotFound:
            with obj_target_cell(instance, cell) as cctxt:
                with compute_utils.notify_about_instance_delete(self.notifier, cctxt, instance, source=fields.NotificationSource.CONDUCTOR):
                    try:
                        instance.destroy()
                    except exception.InstanceNotFound:
                        pass
                    except exception.ObjectActionError:
                        try:
                            instance.refresh()
                            instance.destroy()
                        except exception.InstanceNotFound:
                            pass
            for bdm in instance_bdms:
                with obj_target_cell(bdm, cell):
                    try:
                        bdm.destroy()
                    except exception.ObjectActionError:
                        pass
            if instance_tags:
                with try_target_cell(context, cell) as target_ctxt:
                    try:
                        objects.TagList.destroy(target_ctxt, instance.uuid)
                    except exception.InstanceNotFound:
                        pass
            return False
        return True

    def cache_images(self, context, aggregate, image_ids):
        """Cache a set of images on the set of hosts in an aggregate.

        :param context: The RequestContext
        :param aggregate: The Aggregate object from the request to constrain
                          the host list
        :param image_id: The IDs of the image to cache
        """
        compute_utils.notify_about_aggregate_action(context, aggregate, fields.NotificationAction.IMAGE_CACHE, fields.NotificationPhase.START)
        clock = timeutils.StopWatch()
        threads = CONF.image_cache.precache_concurrency
        fetch_pool = eventlet.GreenPool(size=threads)
        hosts_by_cell = {}
        cells_by_uuid = {}
        for hostname in aggregate.hosts:
            hmap = objects.HostMapping.get_by_host(context, hostname)
            cells_by_uuid.setdefault(hmap.cell_mapping.uuid, hmap.cell_mapping)
            hosts_by_cell.setdefault(hmap.cell_mapping.uuid, [])
            hosts_by_cell[hmap.cell_mapping.uuid].append(hostname)
        LOG.info('Preparing to request pre-caching of image(s) %(image_ids)s on %(hosts)i hosts across %(cells)i cells.', {'image_ids': ','.join(image_ids), 'hosts': len(aggregate.hosts), 'cells': len(hosts_by_cell)})
        clock.start()
        stats = collections.defaultdict(lambda : (0, 0, 0, 0))
        failed_images = collections.defaultdict(int)
        down_hosts = set()
        host_stats = {'completed': 0, 'total': len(aggregate.hosts)}

        def host_completed(context, host, result):
            for (image_id, status) in result.items():
                (cached, existing, error, unsupported) = stats[image_id]
                if status == 'error':
                    failed_images[image_id] += 1
                    error += 1
                elif status == 'cached':
                    cached += 1
                elif status == 'existing':
                    existing += 1
                elif status == 'unsupported':
                    unsupported += 1
                stats[image_id] = (cached, existing, error, unsupported)
            host_stats['completed'] += 1
            compute_utils.notify_about_aggregate_cache(context, aggregate, host, result, host_stats['completed'], host_stats['total'])

        def wrap_cache_images(ctxt, host, image_ids):
            result = self.compute_rpcapi.cache_images(ctxt, host=host, image_ids=image_ids)
            host_completed(context, host, result)

        def skipped_host(context, host, image_ids):
            result = {image: 'skipped' for image in image_ids}
            host_completed(context, host, result)
        for (cell_uuid, hosts) in hosts_by_cell.items():
            cell = cells_by_uuid[cell_uuid]
            with nova_context.target_cell(context, cell) as target_ctxt:
                for host in hosts:
                    service = objects.Service.get_by_compute_host(target_ctxt, host)
                    if not self.servicegroup_api.service_is_up(service):
                        down_hosts.add(host)
                        LOG.info('Skipping image pre-cache request to compute %(host)r because it is not up', {'host': host})
                        skipped_host(target_ctxt, host, image_ids)
                        continue
                    fetch_pool.spawn_n(wrap_cache_images, target_ctxt, host, image_ids)
        fetch_pool.waitall()
        overall_stats = {'cached': 0, 'existing': 0, 'error': 0, 'unsupported': 0}
        for (cached, existing, error, unsupported) in stats.values():
            overall_stats['cached'] += cached
            overall_stats['existing'] += existing
            overall_stats['error'] += error
            overall_stats['unsupported'] += unsupported
        clock.stop()
        LOG.info('Image pre-cache operation for image(s) %(image_ids)s completed in %(time).2f seconds; %(cached)i cached, %(existing)i existing, %(error)i errors, %(unsupported)i unsupported, %(skipped)i skipped (down) hosts', {'image_ids': ','.join(image_ids), 'time': clock.elapsed(), 'cached': overall_stats['cached'], 'existing': overall_stats['existing'], 'error': overall_stats['error'], 'unsupported': overall_stats['unsupported'], 'skipped': len(down_hosts)})
        for (image_id, fails) in failed_images.items():
            LOG.warning('Image pre-cache operation for image %(image)s failed %(fails)i times', {'image': image_id, 'fails': fails})
        compute_utils.notify_about_aggregate_action(context, aggregate, fields.NotificationAction.IMAGE_CACHE, fields.NotificationPhase.END)

    @targets_cell
    @wrap_instance_event(prefix='conductor')
    def confirm_snapshot_based_resize(self, context, instance, migration):
        """Executes the ConfirmResizeTask

        :param context: nova auth request context targeted at the target cell
        :param instance: Instance object in "resized" status from the target
            cell
        :param migration: Migration object from the target cell for the resize
            operation expected to have status "confirming"
        """
        task = cross_cell_migrate.ConfirmResizeTask(context, instance, migration, self.notifier, self.compute_rpcapi)
        task.execute()

    @targets_cell
    @wrap_instance_event(prefix='conductor', graceful_exit=True)
    def revert_snapshot_based_resize(self, context, instance, migration):
        """Executes the RevertResizeTask

        :param context: nova auth request context targeted at the target cell
        :param instance: Instance object in "resized" status from the target
            cell
        :param migration: Migration object from the target cell for the resize
            operation expected to have status "reverting"
        """
        task = cross_cell_migrate.RevertResizeTask(context, instance, migration, self.notifier, self.compute_rpcapi)
        task.execute()