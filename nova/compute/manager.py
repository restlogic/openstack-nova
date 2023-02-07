from bees import profiler as p
'Handles all processes relating to instances (guest vms).\n\nThe :py:class:`ComputeManager` class is a :py:class:`nova.manager.Manager` that\nhandles RPC calls relating to creating instances.  It is responsible for\nbuilding a disk image, launching it via the underlying virtualization driver,\nresponding to calls to check its state, attaching persistent storage, and\nterminating it.\n\n'
import base64
import binascii
import contextlib
import copy
import functools
import inspect
import sys
import time
import traceback
import typing as ty
from cinderclient import exceptions as cinder_exception
from cursive import exception as cursive_exception
import eventlet.event
from eventlet import greenthread
import eventlet.semaphore
import eventlet.timeout
import futurist
from keystoneauth1 import exceptions as keystone_exception
import os_traits
from oslo_log import log as logging
import oslo_messaging as messaging
from oslo_serialization import jsonutils
from oslo_service import loopingcall
from oslo_service import periodic_task
from oslo_utils import excutils
from oslo_utils import strutils
from oslo_utils import timeutils
from oslo_utils import units
from nova.accelerator import cyborg
from nova import block_device
from nova.compute import api as compute
from nova.compute import build_results
from nova.compute import claims
from nova.compute import power_state
from nova.compute import resource_tracker
from nova.compute import rpcapi as compute_rpcapi
from nova.compute import task_states
from nova.compute import utils as compute_utils
from nova.compute.utils import wrap_instance_event
from nova.compute import vm_states
from nova import conductor
import nova.conf
import nova.context
from nova import exception
from nova import exception_wrapper
from nova.i18n import _
from nova.image import glance
from nova import manager
from nova.network import model as network_model
from nova.network import neutron
from nova import objects
from nova.objects import base as obj_base
from nova.objects import external_event as external_event_obj
from nova.objects import fields
from nova.objects import instance as obj_instance
from nova.objects import migrate_data as migrate_data_obj
from nova.pci import request as pci_req_module
from nova.pci import whitelist
from nova import safe_utils
from nova.scheduler.client import query
from nova.scheduler.client import report
from nova.scheduler import utils as scheduler_utils
from nova import utils
from nova.virt import block_device as driver_block_device
from nova.virt import configdrive
from nova.virt import driver
from nova.virt import event as virtevent
from nova.virt import hardware
from nova.virt import storage_users
from nova.virt import virtapi
from nova.volume import cinder
CONF = nova.conf.CONF
LOG = logging.getLogger(__name__)
wrap_exception = functools.partial(exception_wrapper.wrap_exception, service='compute', binary='nova-compute')

@p.trace('errors_out_migration_ctxt')
@contextlib.contextmanager
def errors_out_migration_ctxt(migration):
    """Context manager to error out migration on failure."""
    try:
        yield
    except Exception:
        with excutils.save_and_reraise_exception():
            if migration:
                migration.status = 'error'
                try:
                    migration.save()
                except Exception:
                    LOG.debug('Error setting migration status for instance %s.', migration.instance_uuid, exc_info=True)

@p.trace('errors_out_migration')
@utils.expects_func_args('migration')
def errors_out_migration(function):
    """Decorator to error out migration on failure."""

    @functools.wraps(function)
    def decorated_function(self, context, *args, **kwargs):
        wrapped_func = safe_utils.get_wrapped_function(function)
        keyed_args = inspect.getcallargs(wrapped_func, self, context, *args, **kwargs)
        migration = keyed_args['migration']
        with errors_out_migration_ctxt(migration):
            return function(self, context, *args, **kwargs)
    return decorated_function

@p.trace('reverts_task_state')
@utils.expects_func_args('instance')
def reverts_task_state(function):
    """Decorator to revert task_state on failure."""

    @functools.wraps(function)
    def decorated_function(self, context, *args, **kwargs):
        try:
            return function(self, context, *args, **kwargs)
        except exception.UnexpectedTaskStateError as e:
            with excutils.save_and_reraise_exception():
                LOG.info('Task possibly preempted: %s', e.format_message())
        except Exception:
            with excutils.save_and_reraise_exception():
                wrapped_func = safe_utils.get_wrapped_function(function)
                keyed_args = inspect.getcallargs(wrapped_func, self, context, *args, **kwargs)
                instance = keyed_args['instance']
                original_task_state = instance.task_state
                try:
                    self._instance_update(context, instance, task_state=None)
                    LOG.info('Successfully reverted task state from %s on failure for instance.', original_task_state, instance=instance)
                except exception.InstanceNotFound:
                    pass
                except Exception as e:
                    LOG.warning('Failed to revert task state for instance. Error: %s', e, instance=instance)
    return decorated_function

@p.trace('wrap_instance_fault')
@utils.expects_func_args('instance')
def wrap_instance_fault(function):
    """Wraps a method to catch exceptions related to instances.

    This decorator wraps a method to catch any exceptions having to do with
    an instance that may get thrown. It then logs an instance fault in the db.
    """

    @functools.wraps(function)
    def decorated_function(self, context, *args, **kwargs):
        try:
            return function(self, context, *args, **kwargs)
        except exception.InstanceNotFound:
            raise
        except Exception as e:
            kwargs.update(dict(zip(function.__code__.co_varnames[2:], args)))
            with excutils.save_and_reraise_exception():
                compute_utils.add_instance_fault_from_exc(context, kwargs['instance'], e, sys.exc_info())
    return decorated_function

@p.trace('delete_image_on_error')
@utils.expects_func_args('image_id', 'instance')
def delete_image_on_error(function):
    """Used for snapshot related method to ensure the image created in
    compute.api is deleted when an error occurs.
    """

    @functools.wraps(function)
    def decorated_function(self, context, image_id, instance, *args, **kwargs):
        try:
            return function(self, context, image_id, instance, *args, **kwargs)
        except Exception:
            with excutils.save_and_reraise_exception():
                compute_utils.delete_image(context, instance, self.image_api, image_id, log_exc_info=True)
    return decorated_function
_InstanceEvents = ty.Dict[ty.Tuple[str, str], eventlet.event.Event]

@p.trace_cls('InstanceEvents')
class InstanceEvents(object):

    def __init__(self):
        self._events: ty.Optional[ty.Dict[str, _InstanceEvents]] = {}

    @staticmethod
    def _lock_name(instance) -> str:
        return '%s-%s' % (instance.uuid, 'events')

    def prepare_for_instance_event(self, instance: 'objects.Instance', name: str, tag: str) -> eventlet.event.Event:
        """Prepare to receive an event for an instance.

        This will register an event for the given instance that we will
        wait on later. This should be called before initiating whatever
        action will trigger the event. The resulting eventlet.event.Event
        object should be wait()'d on to ensure completion.

        :param instance: the instance for which the event will be generated
        :param name: the name of the event we're expecting
        :param tag: the tag associated with the event we're expecting
        :returns: an event object that should be wait()'d on
        """

        @utils.synchronized(self._lock_name(instance))
        def _create_or_get_event():
            if self._events is None:
                raise exception.NovaException('In shutdown, no new events can be scheduled')
            instance_events = self._events.setdefault(instance.uuid, {})
            return instance_events.setdefault((name, tag), eventlet.event.Event())
        LOG.debug('Preparing to wait for external event %(name)s-%(tag)s', {'name': name, 'tag': tag}, instance=instance)
        return _create_or_get_event()

    def pop_instance_event(self, instance, event):
        """Remove a pending event from the wait list.

        This will remove a pending event from the wait list so that it
        can be used to signal the waiters to wake up.

        :param instance: the instance for which the event was generated
        :param event: the nova.objects.external_event.InstanceExternalEvent
                      that describes the event
        :returns: the eventlet.event.Event object on which the waiters
                  are blocked
        """
        no_events_sentinel = object()
        no_matching_event_sentinel = object()

        @utils.synchronized(self._lock_name(instance))
        def _pop_event():
            if self._events is None:
                LOG.debug('Unexpected attempt to pop events during shutdown', instance=instance)
                return no_events_sentinel
            events = self._events.get(instance.uuid)
            if not events:
                return no_events_sentinel
            _event = events.pop((event.name, event.tag), None)
            if not events:
                del self._events[instance.uuid]
            if _event is None:
                return no_matching_event_sentinel
            return _event
        result = _pop_event()
        if result is no_events_sentinel:
            LOG.debug('No waiting events found dispatching %(event)s', {'event': event.key}, instance=instance)
            return None
        elif result is no_matching_event_sentinel:
            LOG.debug('No event matching %(event)s in %(events)s', {'event': event.key, 'events': self._events.get(instance.uuid, {}).keys()}, instance=instance)
            return None
        else:
            return result

    def clear_events_for_instance(self, instance):
        """Remove all pending events for an instance.

        This will remove all events currently pending for an instance
        and return them (indexed by event name).

        :param instance: the instance for which events should be purged
        :returns: a dictionary of {event_name: eventlet.event.Event}
        """

        @utils.synchronized(self._lock_name(instance))
        def _clear_events():
            if self._events is None:
                LOG.debug('Unexpected attempt to clear events during shutdown', instance=instance)
                return dict()
            return {'%s-%s' % k: e for (k, e) in self._events.pop(instance.uuid, {}).items()}
        return _clear_events()

    def cancel_all_events(self):
        if self._events is None:
            LOG.debug('Unexpected attempt to cancel events during shutdown.')
            return
        our_events = self._events
        self._events = None
        for (instance_uuid, events) in our_events.items():
            for ((name, tag), eventlet_event) in events.items():
                LOG.debug('Canceling in-flight event %(name)s-%(tag)s for instance %(instance_uuid)s', {'name': name, 'tag': tag, 'instance_uuid': instance_uuid})
                event = objects.InstanceExternalEvent(instance_uuid=instance_uuid, name=name, status='failed', tag=tag, data={})
                eventlet_event.send(event)

@p.trace_cls('ComputeVirtAPI')
class ComputeVirtAPI(virtapi.VirtAPI):

    def __init__(self, compute):
        super(ComputeVirtAPI, self).__init__()
        self._compute = compute
        self.reportclient = compute.reportclient

        class ExitEarly(Exception):

            def __init__(self, events):
                super(Exception, self).__init__()
                self.events = events
        self._exit_early_exc = ExitEarly

    def exit_wait_early(self, events):
        """Exit a wait_for_instance_event() immediately and avoid
        waiting for some events.

        :param: events: A list of (name, tag) tuples for events that we should
                        skip waiting for during a wait_for_instance_event().
        """
        raise self._exit_early_exc(events=events)

    def _default_error_callback(self, event_name, instance):
        raise exception.NovaException(_('Instance event failed'))

    @contextlib.contextmanager
    def wait_for_instance_event(self, instance, event_names, deadline=300, error_callback=None):
        """Plan to wait for some events, run some code, then wait.

        This context manager will first create plans to wait for the
        provided event_names, yield, and then wait for all the scheduled
        events to complete.

        Note that this uses an eventlet.timeout.Timeout to bound the
        operation, so callers should be prepared to catch that
        failure and handle that situation appropriately.

        If the event is not received by the specified timeout deadline,
        eventlet.timeout.Timeout is raised.

        If the event is received but did not have a 'completed'
        status, a NovaException is raised.  If an error_callback is
        provided, instead of raising an exception as detailed above
        for the failure case, the callback will be called with the
        event_name and instance, and can return True to continue
        waiting for the rest of the events, False to stop processing,
        or raise an exception which will bubble up to the waiter.

        If the inner code wishes to abort waiting for one or more
        events because it knows some state to be finished or condition
        to be satisfied, it can use VirtAPI.exit_wait_early() with a
        list of event (name,tag) items to avoid waiting for those
        events upon context exit. Note that exit_wait_early() exits
        the context immediately and should be used to signal that all
        work has been completed and provide the unified list of events
        that need not be waited for. Waiting for the remaining events
        will begin immediately upon early exit as if the context was
        exited normally.

        :param instance: The instance for which an event is expected
        :param event_names: A list of event names. Each element is a
                            tuple of strings to indicate (name, tag),
                            where name is required, but tag may be None.
        :param deadline: Maximum number of seconds we should wait for all
                         of the specified events to arrive.
        :param error_callback: A function to be called if an event arrives

        """
        if error_callback is None:
            error_callback = self._default_error_callback
        events = {}
        for event_name in event_names:
            (name, tag) = event_name
            event_name = objects.InstanceExternalEvent.make_key(name, tag)
            try:
                events[event_name] = self._compute.instance_events.prepare_for_instance_event(instance, name, tag)
            except exception.NovaException:
                error_callback(event_name, instance)
                deadline = 0
        try:
            yield
        except self._exit_early_exc as e:
            early_events = set([objects.InstanceExternalEvent.make_key(n, t) for (n, t) in e.events])
        else:
            early_events = set([])
        with eventlet.timeout.Timeout(deadline):
            for (event_name, event) in events.items():
                if event_name in early_events:
                    continue
                else:
                    actual_event = event.wait()
                    if actual_event.status == 'completed':
                        continue
                decision = error_callback(event_name, instance)
                if decision is False:
                    break

    def update_compute_provider_status(self, context, rp_uuid, enabled):
        """Used to add/remove the COMPUTE_STATUS_DISABLED trait on the provider

        :param context: nova auth RequestContext
        :param rp_uuid: UUID of a compute node resource provider in Placement
        :param enabled: True if the node is enabled in which case the trait
            would be removed, False if the node is disabled in which case
            the trait would be added.
        :raises: ResourceProviderTraitRetrievalFailed
        :raises: ResourceProviderUpdateConflict
        :raises: ResourceProviderUpdateFailed
        :raises: TraitRetrievalFailed
        :raises: keystoneauth1.exceptions.ClientException
        """
        trait_name = os_traits.COMPUTE_STATUS_DISABLED
        trait_info = self.reportclient.get_provider_traits(context, rp_uuid)
        original_traits = trait_info.traits
        new_traits = None
        if enabled and trait_name in original_traits:
            new_traits = original_traits - {trait_name}
            LOG.debug('Removing trait %s from compute node resource provider %s in placement.', trait_name, rp_uuid)
        elif not enabled and trait_name not in original_traits:
            new_traits = original_traits | {trait_name}
            LOG.debug('Adding trait %s to compute node resource provider %s in placement.', trait_name, rp_uuid)
        if new_traits is not None:
            self.reportclient.set_traits_for_provider(context, rp_uuid, new_traits, generation=trait_info.generation)

@p.trace_cls('ComputeManager')
class ComputeManager(manager.Manager):
    """Manages the running instances from creation to destruction."""
    target = messaging.Target(version='6.0')

    def __init__(self, compute_driver=None, *args, **kwargs):
        """Load configuration options and connect to the hypervisor."""
        self.reportclient = report.SchedulerReportClient()
        self.virtapi = ComputeVirtAPI(self)
        self.network_api = neutron.API()
        self.volume_api = cinder.API()
        self.image_api = glance.API()
        self._last_bw_usage_poll = 0.0
        self.compute_api = compute.API()
        self.compute_rpcapi = compute_rpcapi.ComputeAPI()
        self.compute_task_api = conductor.ComputeTaskAPI()
        self.query_client = query.SchedulerQueryClient()
        self.instance_events = InstanceEvents()
        self._sync_power_pool = eventlet.GreenPool(size=CONF.sync_power_state_pool_size)
        self._syncs_in_progress = {}
        self.send_instance_updates = CONF.filter_scheduler.track_instance_changes
        if CONF.max_concurrent_builds != 0:
            self._build_semaphore = eventlet.semaphore.Semaphore(CONF.max_concurrent_builds)
        else:
            self._build_semaphore = compute_utils.UnlimitedSemaphore()
        if CONF.max_concurrent_snapshots > 0:
            self._snapshot_semaphore = eventlet.semaphore.Semaphore(CONF.max_concurrent_snapshots)
        else:
            self._snapshot_semaphore = compute_utils.UnlimitedSemaphore()
        if CONF.max_concurrent_live_migrations > 0:
            self._live_migration_executor = futurist.GreenThreadPoolExecutor(max_workers=CONF.max_concurrent_live_migrations)
        else:
            self._live_migration_executor = futurist.GreenThreadPoolExecutor()
        self._waiting_live_migrations = {}
        super(ComputeManager, self).__init__(*args, service_name='compute', **kwargs)
        self.additional_endpoints.append(_ComputeV5Proxy(self))
        self.driver = driver.load_compute_driver(self.virtapi, compute_driver)
        self.use_legacy_block_device_info = self.driver.need_legacy_block_device_info
        self.rt = resource_tracker.ResourceTracker(self.host, self.driver, reportclient=self.reportclient)

    def reset(self):
        LOG.info('Reloading compute RPC API')
        compute_rpcapi.reset_globals()
        self.compute_rpcapi = compute_rpcapi.ComputeAPI()
        self.reportclient.clear_provider_cache()

    def _update_resource_tracker(self, context, instance):
        """Let the resource tracker know that an instance has changed state."""
        if instance.host == self.host:
            self.rt.update_usage(context, instance, instance.node)

    def _instance_update(self, context, instance, **kwargs):
        """Update an instance in the database using kwargs as value."""
        for (k, v) in kwargs.items():
            setattr(instance, k, v)
        instance.save()
        self._update_resource_tracker(context, instance)

    def _nil_out_instance_obj_host_and_node(self, instance):
        instance.host = None
        instance.node = None
        instance.launched_on = None
        instance.availability_zone = None

    def _set_instance_obj_error_state(self, instance, clean_task_state=False):
        try:
            instance.vm_state = vm_states.ERROR
            if clean_task_state:
                instance.task_state = None
            instance.save()
        except exception.InstanceNotFound:
            LOG.debug('Instance has been destroyed from under us while trying to set it to ERROR', instance=instance)

    def _get_instances_on_driver(self, context, filters=None):
        """Return a list of instance records for the instances found
        on the hypervisor which satisfy the specified filters. If filters=None
        return a list of instance records for all the instances found on the
        hypervisor.
        """
        if not filters:
            filters = {}
        try:
            driver_uuids = self.driver.list_instance_uuids()
            if len(driver_uuids) == 0:
                return objects.InstanceList()
            filters['uuid'] = driver_uuids
            local_instances = objects.InstanceList.get_by_filters(context, filters, use_slave=True)
            return local_instances
        except NotImplementedError:
            pass
        driver_instances = self.driver.list_instances()
        filters['host'] = self.host
        instances = objects.InstanceList.get_by_filters(context, filters, use_slave=True)
        name_map = {instance.name: instance for instance in instances}
        local_instances = []
        for driver_instance in driver_instances:
            instance = name_map.get(driver_instance)
            if not instance:
                continue
            local_instances.append(instance)
        return local_instances

    def _destroy_evacuated_instances(self, context, node_cache):
        """Destroys evacuated instances.

        While nova-compute was down, the instances running on it could be
        evacuated to another host. This method looks for evacuation migration
        records where this is the source host and which were either started
        (accepted), in-progress (pre-migrating) or migrated (done). From those
        migration records, local instances reported by the hypervisor are
        compared to the instances for the migration records and those local
        guests are destroyed, along with instance allocation records in
        Placement for this node.
        Then allocations are removed from Placement for every instance that is
        evacuated from this host regardless if the instance is reported by the
        hypervisor or not.

        :param context: The request context
        :param node_cache: A dict of ComputeNode objects keyed by the UUID of
            the compute node
        :return: A dict keyed by instance uuid mapped to Migration objects
            for instances that were migrated away from this host
        """
        filters = {'source_compute': self.host, 'status': ['accepted', 'pre-migrating', 'done'], 'migration_type': fields.MigrationType.EVACUATION}
        with utils.temporary_mutation(context, read_deleted='yes'):
            evacuations = objects.MigrationList.get_by_filters(context, filters)
        if not evacuations:
            return {}
        evacuations = {mig.instance_uuid: mig for mig in evacuations}
        local_instances = self._get_instances_on_driver(context)
        evacuated_local_instances = {inst.uuid: inst for inst in local_instances if inst.uuid in evacuations}
        for instance in evacuated_local_instances.values():
            LOG.info('Destroying instance as it has been evacuated from this host but still exists in the hypervisor', instance=instance)
            try:
                network_info = self.network_api.get_instance_nw_info(context, instance)
                bdi = self._get_instance_block_device_info(context, instance)
                destroy_disks = not self._is_instance_storage_shared(context, instance)
            except exception.InstanceNotFound:
                network_info = network_model.NetworkInfo()
                bdi = {}
                LOG.info('Instance has been marked deleted already, removing it from the hypervisor.', instance=instance)
                destroy_disks = True
            self.driver.destroy(context, instance, network_info, bdi, destroy_disks)
        hostname_to_cn_uuid = {cn.hypervisor_hostname: cn.uuid for cn in node_cache.values()}
        for (instance_uuid, migration) in evacuations.items():
            try:
                if instance_uuid in evacuated_local_instances:
                    instance = evacuated_local_instances[instance_uuid]
                else:
                    instance = objects.Instance.get_by_uuid(context, instance_uuid)
            except exception.InstanceNotFound:
                continue
            LOG.info('Cleaning up allocations of the instance as it has been evacuated from this host', instance=instance)
            if migration.source_node not in hostname_to_cn_uuid:
                LOG.error('Failed to clean allocation of evacuated instance as the source node %s is not found', migration.source_node, instance=instance)
                continue
            cn_uuid = hostname_to_cn_uuid[migration.source_node]
            if not instance.deleted and (not self.reportclient.remove_provider_tree_from_instance_allocation(context, instance.uuid, cn_uuid)):
                LOG.error('Failed to clean allocation of evacuated instance on the source node %s', cn_uuid, instance=instance)
            migration.status = 'completed'
            migration.save()
        return evacuations

    def _is_instance_storage_shared(self, context, instance, host=None):
        shared_storage = True
        data = None
        try:
            data = self.driver.check_instance_shared_storage_local(context, instance)
            if data:
                shared_storage = self.compute_rpcapi.check_instance_shared_storage(context, data, instance=instance, host=host)
        except NotImplementedError:
            LOG.debug("Hypervisor driver does not support instance shared storage check, assuming it's not on shared storage", instance=instance)
            shared_storage = False
        except Exception:
            LOG.exception('Failed to check if instance shared', instance=instance)
        finally:
            if data:
                self.driver.check_instance_shared_storage_cleanup(context, data)
        return shared_storage

    def _complete_partial_deletion(self, context, instance):
        """Complete deletion for instances in DELETED status but not marked as
        deleted in the DB
        """
        instance.destroy()
        bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(context, instance.uuid)
        self._complete_deletion(context, instance)
        self._notify_about_instance_usage(context, instance, 'delete.end')
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.DELETE, phase=fields.NotificationPhase.END, bdms=bdms)

    def _complete_deletion(self, context, instance):
        self._update_resource_tracker(context, instance)
        self.reportclient.delete_allocation_for_instance(context, instance.uuid)
        self._clean_instance_console_tokens(context, instance)
        self._delete_scheduler_instance_info(context, instance.uuid)

    def _validate_pinning_configuration(self, instances):
        if not self.driver.capabilities.get('supports_pcpus', False):
            return
        for instance in instances:
            if instance.deleted:
                continue
            if not instance.numa_topology or instance.numa_topology.cpu_policy in (None, fields.CPUAllocationPolicy.SHARED):
                if CONF.compute.cpu_dedicated_set and (not CONF.compute.cpu_shared_set):
                    msg = _("This host has unpinned instances but has no CPUs set aside for this purpose; configure '[compute] cpu_shared_set' instead of, or in addition to, '[compute] cpu_dedicated_set'")
                    raise exception.InvalidConfiguration(msg)
                continue
            if CONF.compute.cpu_shared_set and (not CONF.compute.cpu_dedicated_set) and (not CONF.vcpu_pin_set):
                msg = _("This host has pinned instances but has no CPUs set aside for this purpose; configure '[compute] cpu_dedicated_set' instead of, or in addition to, '[compute] cpu_shared_set'.")
                raise exception.InvalidConfiguration(msg)
            if instance.numa_topology.cpu_policy == fields.CPUAllocationPolicy.MIXED and (not CONF.compute.cpu_shared_set):
                msg = _("This host has mixed instance requesting both pinned and unpinned CPUs but hasn't set aside unpinned CPUs for this purpose; Configure '[compute] cpu_shared_set'.")
                raise exception.InvalidConfiguration(msg)
            if instance.numa_topology.cpu_policy == fields.CPUAllocationPolicy.MIXED and (not CONF.compute.cpu_dedicated_set):
                msg = _("This host has mixed instance requesting both pinned and unpinned CPUs but hasn't set aside pinned CPUs for this purpose; Configure '[compute] cpu_dedicated_set'")
                raise exception.InvalidConfiguration(msg)
            available_dedicated_cpus = hardware.get_vcpu_pin_set() or hardware.get_cpu_dedicated_set()
            pinned_cpus = instance.numa_topology.cpu_pinning
            if available_dedicated_cpus and pinned_cpus - available_dedicated_cpus:
                LOG.warning("Instance is pinned to host CPUs %(cpus)s but one or more of these CPUs are not included in either '[compute] cpu_dedicated_set' or 'vcpu_pin_set'; you should update these configuration options to include the missing CPUs or rebuild or cold migrate this instance.", {'cpus': list(pinned_cpus)}, instance=instance)

    def _validate_vtpm_configuration(self, instances):
        if self.driver.capabilities.get('supports_vtpm', False):
            return
        for instance in instances:
            if instance.deleted:
                continue
            if hardware.get_vtpm_constraint(instance.flavor, instance.image_meta):
                msg = _('This host has instances with the vTPM feature enabled, but the host is not correctly configured; enable vTPM support.')
                raise exception.InvalidConfiguration(msg)

    def _reset_live_migration(self, context, instance):
        migration = None
        try:
            migration = objects.Migration.get_by_instance_and_status(context, instance.uuid, 'running')
            if migration:
                self.live_migration_abort(context, instance, migration.id)
        except Exception:
            LOG.exception('Failed to abort live-migration', instance=instance)
        finally:
            if migration:
                self._set_migration_status(migration, 'error')
            LOG.info('Instance found in migrating state during startup. Resetting task_state', instance=instance)
            instance.task_state = None
            instance.save(expected_task_state=[task_states.MIGRATING])

    def _init_instance(self, context, instance):
        """Initialize this instance during service init."""
        if instance.host != self.host:
            LOG.warning('Instance %(uuid)s appears to not be owned by this host, but by %(host)s. Startup processing is being skipped.', {'uuid': instance.uuid, 'host': instance.host})
            return
        if instance.vm_state == vm_states.SOFT_DELETED or (instance.vm_state == vm_states.ERROR and instance.task_state not in (task_states.RESIZE_MIGRATING, task_states.DELETING)):
            LOG.debug('Instance is in %s state.', instance.vm_state, instance=instance)
            return
        if instance.vm_state == vm_states.DELETED:
            try:
                self._complete_partial_deletion(context, instance)
            except Exception:
                LOG.exception('Failed to complete a deletion', instance=instance)
            return
        if instance.vm_state == vm_states.BUILDING or instance.task_state in [task_states.SCHEDULING, task_states.BLOCK_DEVICE_MAPPING, task_states.NETWORKING, task_states.SPAWNING]:
            LOG.debug('Instance failed to spawn correctly, setting to ERROR state', instance=instance)
            self._set_instance_obj_error_state(instance, clean_task_state=True)
            return
        if instance.vm_state in [vm_states.ACTIVE, vm_states.STOPPED] and instance.task_state in [task_states.REBUILDING, task_states.REBUILD_BLOCK_DEVICE_MAPPING, task_states.REBUILD_SPAWNING]:
            LOG.debug('Instance failed to rebuild correctly, setting to ERROR state', instance=instance)
            self._set_instance_obj_error_state(instance, clean_task_state=True)
            return
        if instance.vm_state != vm_states.ERROR and instance.task_state in [task_states.IMAGE_SNAPSHOT_PENDING, task_states.IMAGE_PENDING_UPLOAD, task_states.IMAGE_UPLOADING, task_states.IMAGE_SNAPSHOT]:
            LOG.debug('Instance in transitional state %s at start-up clearing task state', instance.task_state, instance=instance)
            instance.task_state = None
            instance.save()
        if instance.vm_state != vm_states.ERROR and instance.task_state in [task_states.RESIZE_PREP]:
            LOG.debug('Instance in transitional state %s at start-up clearing task state', instance['task_state'], instance=instance)
            instance.task_state = None
            instance.save()
        if instance.task_state == task_states.DELETING:
            try:
                LOG.info('Service started deleting the instance during the previous run, but did not finish. Restarting the deletion now.', instance=instance)
                instance.obj_load_attr('metadata')
                instance.obj_load_attr('system_metadata')
                bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(context, instance.uuid)
                self._delete_instance(context, instance, bdms)
            except Exception:
                LOG.exception('Failed to complete a deletion', instance=instance)
                self._set_instance_obj_error_state(instance)
            return
        current_power_state = self._get_power_state(instance)
        (try_reboot, reboot_type) = self._retry_reboot(instance, current_power_state)
        if try_reboot:
            LOG.debug('Instance in transitional state (%(task_state)s) at start-up and power state is (%(power_state)s), triggering reboot', {'task_state': instance.task_state, 'power_state': current_power_state}, instance=instance)
            if instance.task_state in task_states.soft_reboot_states and reboot_type == 'HARD':
                instance.task_state = task_states.REBOOT_PENDING_HARD
                instance.save()
            self.reboot_instance(context, instance, block_device_info=None, reboot_type=reboot_type)
            return
        elif current_power_state == power_state.RUNNING and instance.task_state in [task_states.REBOOT_STARTED, task_states.REBOOT_STARTED_HARD, task_states.PAUSING, task_states.UNPAUSING]:
            LOG.warning('Instance in transitional state (%(task_state)s) at start-up and power state is (%(power_state)s), clearing task state', {'task_state': instance.task_state, 'power_state': current_power_state}, instance=instance)
            instance.task_state = None
            instance.vm_state = vm_states.ACTIVE
            instance.save()
        elif current_power_state == power_state.PAUSED and instance.task_state == task_states.UNPAUSING:
            LOG.warning('Instance in transitional state (%(task_state)s) at start-up and power state is (%(power_state)s), clearing task state and unpausing the instance', {'task_state': instance.task_state, 'power_state': current_power_state}, instance=instance)
            try:
                self.unpause_instance(context, instance)
            except NotImplementedError:
                pass
            except Exception:
                LOG.exception('Failed to unpause instance', instance=instance)
            return
        if instance.task_state == task_states.POWERING_OFF:
            try:
                LOG.debug('Instance in transitional state %s at start-up retrying stop request', instance.task_state, instance=instance)
                self.stop_instance(context, instance, True)
            except Exception:
                LOG.exception('Failed to stop instance', instance=instance)
            return
        if instance.task_state == task_states.POWERING_ON:
            try:
                LOG.debug('Instance in transitional state %s at start-up retrying start request', instance.task_state, instance=instance)
                self.start_instance(context, instance)
            except Exception:
                LOG.exception('Failed to start instance', instance=instance)
            return
        net_info = instance.get_network_info()
        try:
            self.driver.plug_vifs(instance, net_info)
        except NotImplementedError as e:
            LOG.debug(e, instance=instance)
        except exception.VirtualInterfacePlugException:
            LOG.exception('Virtual interface plugging failed for instance. The port binding:host_id may need to be manually updated.', instance=instance)
            self._set_instance_obj_error_state(instance)
            return
        if instance.task_state == task_states.RESIZE_MIGRATING:
            try:
                power_on = instance.system_metadata.get('old_vm_state') != vm_states.STOPPED
                block_dev_info = self._get_instance_block_device_info(context, instance)
                migration = objects.Migration.get_by_id_and_instance(context, instance.migration_context.migration_id, instance.uuid)
                self.driver.finish_revert_migration(context, instance, net_info, migration, block_dev_info, power_on)
            except Exception:
                LOG.exception('Failed to revert crashed migration', instance=instance)
            finally:
                LOG.info('Instance found in migrating state during startup. Resetting task_state', instance=instance)
                instance.task_state = None
                instance.save()
        if instance.task_state == task_states.MIGRATING:
            self._reset_live_migration(context, instance)
        db_state = instance.power_state
        drv_state = self._get_power_state(instance)
        expect_running = db_state == power_state.RUNNING and drv_state != db_state
        LOG.debug('Current state is %(drv_state)s, state in DB is %(db_state)s.', {'drv_state': drv_state, 'db_state': db_state}, instance=instance)
        if expect_running and CONF.resume_guests_state_on_host_boot:
            self._resume_guests_state(context, instance, net_info)

    def _resume_guests_state(self, context, instance, net_info):
        LOG.info('Rebooting instance after nova-compute restart.', instance=instance)
        block_device_info = self._get_instance_block_device_info(context, instance)
        try:
            self.driver.resume_state_on_host_boot(context, instance, net_info, block_device_info)
        except NotImplementedError:
            LOG.warning('Hypervisor driver does not support resume guests', instance=instance)
        except Exception:
            LOG.warning('Failed to resume instance', instance=instance)
            self._set_instance_obj_error_state(instance)

    def _retry_reboot(self, instance, current_power_state):
        current_task_state = instance.task_state
        retry_reboot = False
        reboot_type = compute_utils.get_reboot_type(current_task_state, current_power_state)
        pending_soft = current_task_state == task_states.REBOOT_PENDING and instance.vm_state in vm_states.ALLOW_SOFT_REBOOT
        pending_hard = current_task_state == task_states.REBOOT_PENDING_HARD and instance.vm_state in vm_states.ALLOW_HARD_REBOOT
        started_not_running = current_task_state in [task_states.REBOOT_STARTED, task_states.REBOOT_STARTED_HARD] and current_power_state != power_state.RUNNING
        if pending_soft or pending_hard or started_not_running:
            retry_reboot = True
        return (retry_reboot, reboot_type)

    def handle_lifecycle_event(self, event):
        LOG.info('VM %(state)s (Lifecycle Event)', {'state': event.get_name()}, instance_uuid=event.get_instance_uuid())
        context = nova.context.get_admin_context(read_deleted='yes')
        vm_power_state = None
        event_transition = event.get_transition()
        if event_transition == virtevent.EVENT_LIFECYCLE_STOPPED:
            vm_power_state = power_state.SHUTDOWN
        elif event_transition == virtevent.EVENT_LIFECYCLE_STARTED:
            vm_power_state = power_state.RUNNING
        elif event_transition in (virtevent.EVENT_LIFECYCLE_PAUSED, virtevent.EVENT_LIFECYCLE_POSTCOPY_STARTED, virtevent.EVENT_LIFECYCLE_MIGRATION_COMPLETED):
            vm_power_state = power_state.PAUSED
        elif event_transition == virtevent.EVENT_LIFECYCLE_RESUMED:
            vm_power_state = power_state.RUNNING
        elif event_transition == virtevent.EVENT_LIFECYCLE_SUSPENDED:
            vm_power_state = power_state.SUSPENDED
        else:
            LOG.warning('Unexpected lifecycle event: %d', event_transition)
        migrate_finish_statuses = {virtevent.EVENT_LIFECYCLE_POSTCOPY_STARTED: 'running (post-copy)', virtevent.EVENT_LIFECYCLE_MIGRATION_COMPLETED: 'running'}
        expected_attrs = []
        if event_transition in migrate_finish_statuses:
            expected_attrs.append('info_cache')
        instance = objects.Instance.get_by_uuid(context, event.get_instance_uuid(), expected_attrs=expected_attrs)
        current_power_state = self._get_power_state(instance)
        if current_power_state == vm_power_state:
            LOG.debug('Synchronizing instance power state after lifecycle event "%(event)s"; current vm_state: %(vm_state)s, current task_state: %(task_state)s, current DB power_state: %(db_power_state)s, VM power_state: %(vm_power_state)s', {'event': event.get_name(), 'vm_state': instance.vm_state, 'task_state': instance.task_state, 'db_power_state': instance.power_state, 'vm_power_state': vm_power_state}, instance_uuid=instance.uuid)
            self._sync_instance_power_state(context, instance, vm_power_state)
        if instance.task_state == task_states.MIGRATING and event_transition in migrate_finish_statuses:
            status = migrate_finish_statuses[event_transition]
            try:
                migration = objects.Migration.get_by_instance_and_status(context, instance.uuid, status)
                LOG.debug('Binding ports to destination host: %s', migration.dest_compute, instance=instance)
                self.network_api.migrate_instance_start(context, instance, migration)
            except exception.MigrationNotFoundByStatus:
                LOG.warning("Unable to find migration record with status '%s' for instance. Port binding will happen in post live migration.", status, instance=instance)

    def handle_events(self, event):
        if isinstance(event, virtevent.LifecycleEvent):
            try:
                self.handle_lifecycle_event(event)
            except exception.InstanceNotFound:
                LOG.debug('Event %s arrived for non-existent instance. The instance was probably deleted.', event)
        else:
            LOG.debug('Ignoring event %s', event)

    def init_virt_events(self):
        if CONF.workarounds.handle_virt_lifecycle_events:
            self.driver.register_event_listener(self.handle_events)
        elif CONF.sync_power_state_interval < 0:
            LOG.warning('Instance lifecycle events from the compute driver have been disabled. Note that lifecycle changes to an instance outside of the compute service will not be synchronized automatically since the _sync_power_states periodic task is also disabled.')
        else:
            LOG.info('Instance lifecycle events from the compute driver have been disabled. Note that lifecycle changes to an instance outside of the compute service will only be synchronized by the _sync_power_states periodic task.')

    def _get_nodes(self, context):
        """Queried the ComputeNode objects from the DB that are reported by the
        hypervisor.

        :param context: the request context
        :return: a dict of ComputeNode objects keyed by the UUID of the given
            node.
        """
        nodes_by_uuid = {}
        try:
            node_names = self.driver.get_available_nodes()
        except exception.VirtDriverNotReady:
            LOG.warning('Virt driver is not ready. If this is the first time this service is starting on this host, then you can ignore this warning.')
            return {}
        for node_name in node_names:
            try:
                node = objects.ComputeNode.get_by_host_and_nodename(context, self.host, node_name)
                nodes_by_uuid[node.uuid] = node
            except exception.ComputeHostNotFound:
                LOG.warning('Compute node %s not found in the database. If this is the first time this service is starting on this host, then you can ignore this warning.', node_name)
        return nodes_by_uuid

    def init_host(self):
        """Initialization for a standalone compute service."""
        if CONF.pci.passthrough_whitelist:
            whitelist.Whitelist(CONF.pci.passthrough_whitelist)
        nova.conf.neutron.register_dynamic_opts(CONF)
        nova.conf.devices.register_dynamic_opts(CONF)
        if CONF.compute.max_concurrent_disk_ops != 0:
            compute_utils.disk_ops_semaphore = eventlet.semaphore.BoundedSemaphore(CONF.compute.max_concurrent_disk_ops)
        if CONF.compute.max_disk_devices_to_attach == 0:
            msg = _('[compute]max_disk_devices_to_attach has been set to 0, which will prevent instances from being able to boot. Set -1 for unlimited or set >= 1 to limit the maximum number of disk devices.')
            raise exception.InvalidConfiguration(msg)
        self.driver.init_host(host=self.host)
        context = nova.context.get_admin_context()
        instances = objects.InstanceList.get_by_host(context, self.host, expected_attrs=['info_cache', 'metadata', 'numa_topology'])
        self.init_virt_events()
        self._validate_pinning_configuration(instances)
        self._validate_vtpm_configuration(instances)
        nodes_by_uuid = self._get_nodes(context)
        try:
            evacuated_instances = self._destroy_evacuated_instances(context, nodes_by_uuid)
            for instance in instances:
                if instance.uuid not in evacuated_instances:
                    self._init_instance(context, instance)
            already_handled = {instance.uuid for instance in instances}.union(evacuated_instances)
            self._error_out_instances_whose_build_was_interrupted(context, already_handled, nodes_by_uuid.keys())
        finally:
            if instances:
                self._update_scheduler_instance_info(context, instances)

    def _error_out_instances_whose_build_was_interrupted(self, context, already_handled_instances, node_uuids):
        """If there are instances in BUILDING state that are not
        assigned to this host but have allocations in placement towards
        this compute that means the nova-compute service was
        restarted while those instances waited for the resource claim
        to finish and the _set_instance_host_and_node() to update the
        instance.host field. We need to push them to ERROR state here to
        prevent keeping them in BUILDING state forever.

        :param context: The request context
        :param already_handled_instances: The set of instance UUIDs that the
            host initialization process already handled in some way.
        :param node_uuids: The list of compute node uuids handled by this
            service
        """
        LOG.info('Looking for unclaimed instances stuck in BUILDING status for nodes managed by this host')
        for cn_uuid in node_uuids:
            try:
                f = self.reportclient.get_allocations_for_resource_provider
                allocations = f(context, cn_uuid).allocations
            except (exception.ResourceProviderAllocationRetrievalFailed, keystone_exception.ClientException) as e:
                LOG.error('Could not retrieve compute node resource provider %s and therefore unable to error out any instances stuck in BUILDING state. Error: %s', cn_uuid, str(e))
                continue
            not_handled_consumers = set(allocations) - already_handled_instances
            if not not_handled_consumers:
                continue
            filters = {'vm_state': vm_states.BUILDING, 'uuid': not_handled_consumers}
            instances = objects.InstanceList.get_by_filters(context, filters, expected_attrs=[])
            for instance in instances:
                LOG.debug('Instance spawn was interrupted before instance_claim, setting instance to ERROR state', instance=instance)
                self._set_instance_obj_error_state(instance, clean_task_state=True)

    def cleanup_host(self):
        self.driver.register_event_listener(None)
        self.instance_events.cancel_all_events()
        self.driver.cleanup_host(host=self.host)
        self._cleanup_live_migrations_in_pool()

    def _cleanup_live_migrations_in_pool(self):
        self._live_migration_executor.shutdown(wait=False)
        for (migration, future) in self._waiting_live_migrations.values():
            if future is None:
                continue
            if future.cancel():
                self._set_migration_status(migration, 'cancelled')
                LOG.info('Successfully cancelled queued live migration.', instance_uuid=migration.instance_uuid)
            else:
                LOG.warning('Unable to cancel live migration.', instance_uuid=migration.instance_uuid)
        self._waiting_live_migrations.clear()

    def pre_start_hook(self):
        """After the service is initialized, but before we fully bring
        the service up by listening on RPC queues, make sure to update
        our available resources (and indirectly our available nodes).
        """
        self.update_available_resource(nova.context.get_admin_context(), startup=True)

    def _get_power_state(self, instance):
        """Retrieve the power state for the given instance."""
        LOG.debug('Checking state', instance=instance)
        try:
            return self.driver.get_info(instance, use_cache=False).state
        except exception.InstanceNotFound:
            return power_state.NOSTATE

    def _await_block_device_map_created(self, context, vol_id):
        start = time.time()
        retries = CONF.block_device_allocate_retries
        attempts = 1
        if retries >= 1:
            attempts = retries + 1
        for attempt in range(1, attempts + 1):
            volume = self.volume_api.get(context, vol_id)
            volume_status = volume['status']
            if volume_status not in ['creating', 'downloading']:
                if volume_status == 'available':
                    return attempt
                LOG.warning('Volume id: %(vol_id)s finished being created but its status is %(vol_status)s.', {'vol_id': vol_id, 'vol_status': volume_status})
                break
            greenthread.sleep(CONF.block_device_allocate_retries_interval)
        raise exception.VolumeNotCreated(volume_id=vol_id, seconds=int(time.time() - start), attempts=attempt, volume_status=volume_status)

    def _decode_files(self, injected_files):
        """Base64 decode the list of files to inject."""
        if not injected_files:
            return []

        def _decode(f):
            (path, contents) = f
            try:
                decoded = base64.b64decode(contents)
                return (path, decoded)
            except (TypeError, binascii.Error):
                raise exception.Base64Exception(path=path)
        return [_decode(f) for f in injected_files]

    def _validate_instance_group_policy(self, context, instance, scheduler_hints=None):
        if CONF.workarounds.disable_group_policy_check_upcall:
            return
        if scheduler_hints is not None:
            group_hint = scheduler_hints.get('group')
            if not group_hint:
                return
            else:
                if isinstance(group_hint, list):
                    group_hint = group_hint[0]
                group = objects.InstanceGroup.get_by_hint(context, group_hint)
        else:
            try:
                group = objects.InstanceGroup.get_by_instance_uuid(context, instance.uuid)
            except exception.InstanceGroupNotFound:
                return

        @utils.synchronized(group['uuid'])
        def _do_validation(context, instance, group):
            if group.policy and 'anti-affinity' == group.policy:
                instances_uuids = objects.InstanceList.get_uuids_by_host(context, self.host)
                ins_on_host = set(instances_uuids)
                nodename = self._get_nodename(instance)
                migrations = objects.MigrationList.get_in_progress_by_host_and_node(context, self.host, nodename)
                migration_vm_uuids = set([mig['instance_uuid'] for mig in migrations])
                total_instances = migration_vm_uuids | ins_on_host
                group = objects.InstanceGroup.get_by_uuid(context, group['uuid'])
                members = set(group.members)
                members_on_host = total_instances & members - set([instance.uuid])
                rules = group.rules
                if rules and 'max_server_per_host' in rules:
                    max_server = rules['max_server_per_host']
                else:
                    max_server = 1
                if len(members_on_host) >= max_server:
                    msg = _('Anti-affinity instance group policy was violated.')
                    raise exception.RescheduledException(instance_uuid=instance.uuid, reason=msg)
            elif group.policy and 'affinity' == group.policy:
                group_hosts = group.get_hosts(exclude=[instance.uuid])
                if group_hosts and self.host not in group_hosts:
                    msg = _('Affinity instance group policy was violated.')
                    raise exception.RescheduledException(instance_uuid=instance.uuid, reason=msg)
        _do_validation(context, instance, group)

    def _log_original_error(self, exc_info, instance_uuid):
        LOG.error('Error: %s', exc_info[1], instance_uuid=instance_uuid, exc_info=exc_info)

    @periodic_task.periodic_task
    def _check_instance_build_time(self, context):
        """Ensure that instances are not stuck in build."""
        timeout = CONF.instance_build_timeout
        if timeout == 0:
            return
        filters = {'vm_state': vm_states.BUILDING, 'host': self.host}
        building_insts = objects.InstanceList.get_by_filters(context, filters, expected_attrs=[], use_slave=True)
        for instance in building_insts:
            if timeutils.is_older_than(instance.created_at, timeout):
                self._set_instance_obj_error_state(instance)
                LOG.warning('Instance build timed out. Set to error state.', instance=instance)

    def _check_instance_exists(self, instance):
        """Ensure an instance with the same name is not already present."""
        if self.driver.instance_exists(instance):
            raise exception.InstanceExists(name=instance.name)

    def _allocate_network_async(self, context, instance, requested_networks, security_groups, resource_provider_mapping):
        """Method used to allocate networks in the background.

        Broken out for testing.
        """
        if requested_networks and requested_networks.no_allocate:
            LOG.debug("Not allocating networking since 'none' was specified.", instance=instance)
            return network_model.NetworkInfo([])
        LOG.debug('Allocating IP information in the background.', instance=instance)
        retries = CONF.network_allocate_retries
        attempts = retries + 1
        retry_time = 1
        bind_host_id = self.driver.network_binding_host_id(context, instance)
        for attempt in range(1, attempts + 1):
            try:
                nwinfo = self.network_api.allocate_for_instance(context, instance, requested_networks=requested_networks, security_groups=security_groups, bind_host_id=bind_host_id, resource_provider_mapping=resource_provider_mapping)
                LOG.debug('Instance network_info: |%s|', nwinfo, instance=instance)
                instance.system_metadata['network_allocated'] = 'True'
                return nwinfo
            except Exception as e:
                log_info = {'attempt': attempt, 'attempts': attempts}
                if attempt == attempts:
                    LOG.exception('Instance failed network setup after %(attempts)d attempt(s)', log_info)
                    raise e
                LOG.warning('Instance failed network setup (attempt %(attempt)d of %(attempts)d)', log_info, instance=instance)
                time.sleep(retry_time)
                retry_time *= 2
                if retry_time > 30:
                    retry_time = 30

    def _build_networks_for_instance(self, context, instance, requested_networks, security_groups, resource_provider_mapping):
        if strutils.bool_from_string(instance.system_metadata.get('network_allocated', 'False')):
            self.network_api.setup_instance_network_on_host(context, instance, instance.host)
            return self.network_api.get_instance_nw_info(context, instance)
        network_info = self._allocate_network(context, instance, requested_networks, security_groups, resource_provider_mapping)
        return network_info

    def _allocate_network(self, context, instance, requested_networks, security_groups, resource_provider_mapping):
        """Start network allocation asynchronously.  Return an instance
        of NetworkInfoAsyncWrapper that can be used to retrieve the
        allocated networks when the operation has finished.
        """
        instance.vm_state = vm_states.BUILDING
        instance.task_state = task_states.NETWORKING
        instance.save(expected_task_state=[None])
        return network_model.NetworkInfoAsyncWrapper(self._allocate_network_async, context, instance, requested_networks, security_groups, resource_provider_mapping)

    def _default_root_device_name(self, instance, image_meta, root_bdm):
        """Gets a default root device name from the driver.

        :param nova.objects.Instance instance:
            The instance for which to get the root device name.
        :param nova.objects.ImageMeta image_meta:
            The metadata of the image of the instance.
        :param nova.objects.BlockDeviceMapping root_bdm:
            The description of the root device.
        :returns: str -- The default root device name.
        :raises: InternalError, TooManyDiskDevices
        """
        try:
            return self.driver.default_root_device_name(instance, image_meta, root_bdm)
        except NotImplementedError:
            return compute_utils.get_next_device_name(instance, [])

    def _default_device_names_for_instance(self, instance, root_device_name, *block_device_lists):
        """Default the missing device names in the BDM from the driver.

        :param nova.objects.Instance instance:
            The instance for which to get default device names.
        :param str root_device_name: The root device name.
        :param list block_device_lists: List of block device mappings.
        :returns: None
        :raises: InternalError, TooManyDiskDevices
        """
        try:
            self.driver.default_device_names_for_instance(instance, root_device_name, *block_device_lists)
        except NotImplementedError:
            compute_utils.default_device_names_for_instance(instance, root_device_name, *block_device_lists)

    def _get_device_name_for_instance(self, instance, bdms, block_device_obj):
        """Get the next device name from the driver, based on the BDM.

        :param nova.objects.Instance instance:
            The instance whose volume is requesting a device name.
        :param nova.objects.BlockDeviceMappingList bdms:
            The block device mappings for the instance.
        :param nova.objects.BlockDeviceMapping block_device_obj:
            A block device mapping containing info about the requested block
            device.
        :returns: The next device name.
        :raises: InternalError, TooManyDiskDevices
        """
        block_device_obj = block_device_obj.obj_clone()
        try:
            return self.driver.get_device_name_for_instance(instance, bdms, block_device_obj)
        except NotImplementedError:
            return compute_utils.get_device_name_for_instance(instance, bdms, block_device_obj.get('device_name'))

    def _default_block_device_names(self, instance, image_meta, block_devices):
        """Verify that all the devices have the device_name set. If not,
        provide a default name.

        It also ensures that there is a root_device_name and is set to the
        first block device in the boot sequence (boot_index=0).
        """
        root_bdm = block_device.get_root_bdm(block_devices)
        if not root_bdm:
            return
        root_device_name = None
        update_root_bdm = False
        if root_bdm.device_name:
            root_device_name = root_bdm.device_name
            instance.root_device_name = root_device_name
        elif instance.root_device_name:
            root_device_name = instance.root_device_name
            root_bdm.device_name = root_device_name
            update_root_bdm = True
        else:
            root_device_name = self._default_root_device_name(instance, image_meta, root_bdm)
            instance.root_device_name = root_device_name
            root_bdm.device_name = root_device_name
            update_root_bdm = True
        if update_root_bdm:
            root_bdm.save()
        ephemerals = []
        swap = []
        block_device_mapping = []
        for device in block_devices:
            if block_device.new_format_is_ephemeral(device):
                ephemerals.append(device)
            if block_device.new_format_is_swap(device):
                swap.append(device)
            if driver_block_device.is_block_device_mapping(device):
                block_device_mapping.append(device)
        self._default_device_names_for_instance(instance, root_device_name, ephemerals, swap, block_device_mapping)

    def _block_device_info_to_legacy(self, block_device_info):
        """Convert BDI to the old format for drivers that need it."""
        if self.use_legacy_block_device_info:
            ephemerals = driver_block_device.legacy_block_devices(driver.block_device_info_get_ephemerals(block_device_info))
            mapping = driver_block_device.legacy_block_devices(driver.block_device_info_get_mapping(block_device_info))
            swap = block_device_info['swap']
            if swap:
                swap = swap.legacy()
            block_device_info.update({'ephemerals': ephemerals, 'swap': swap, 'block_device_mapping': mapping})

    def _add_missing_dev_names(self, bdms, instance):
        for bdm in bdms:
            if bdm.device_name is not None:
                continue
            device_name = self._get_device_name_for_instance(instance, bdms, bdm)
            values = {'device_name': device_name}
            bdm.update(values)
            bdm.save()

    def _prep_block_device(self, context, instance, bdms):
        """Set up the block device for an instance with error logging."""
        try:
            self._add_missing_dev_names(bdms, instance)
            block_device_info = driver.get_block_device_info(instance, bdms)
            mapping = driver.block_device_info_get_mapping(block_device_info)
            driver_block_device.attach_block_devices(mapping, context, instance, self.volume_api, self.driver, wait_func=self._await_block_device_map_created)
            self._block_device_info_to_legacy(block_device_info)
            return block_device_info
        except exception.OverQuota as e:
            LOG.warning('Failed to create block device for instance due to exceeding volume related resource quota. Error: %s', e.message, instance=instance)
            raise
        except Exception as ex:
            LOG.exception('Instance failed block device setup', instance=instance)
            raise exception.InvalidBDM(str(ex))

    def _update_instance_after_spawn(self, instance, vm_state=vm_states.ACTIVE):
        instance.power_state = self._get_power_state(instance)
        instance.vm_state = vm_state
        instance.task_state = None
        configdrive.update_instance(instance)
        instance.launched_at = timeutils.utcnow()

    def _update_scheduler_instance_info(self, context, instance):
        """Sends an InstanceList with created or updated Instance objects to
        the Scheduler client.

        In the case of init_host, the value passed will already be an
        InstanceList. Other calls will send individual Instance objects that
        have been created or resized. In this case, we create an InstanceList
        object containing that Instance.
        """
        if not self.send_instance_updates:
            return
        if isinstance(instance, obj_instance.Instance):
            instance = objects.InstanceList(objects=[instance])
        context = context.elevated()
        self.query_client.update_instance_info(context, self.host, instance)

    def _delete_scheduler_instance_info(self, context, instance_uuid):
        """Sends the uuid of the deleted Instance to the Scheduler client."""
        if not self.send_instance_updates:
            return
        context = context.elevated()
        self.query_client.delete_instance_info(context, self.host, instance_uuid)

    @periodic_task.periodic_task(spacing=CONF.scheduler_instance_sync_interval)
    def _sync_scheduler_instance_info(self, context):
        if not self.send_instance_updates:
            return
        context = context.elevated()
        instances = objects.InstanceList.get_by_host(context, self.host, expected_attrs=[], use_slave=True)
        uuids = [instance.uuid for instance in instances]
        self.query_client.sync_instance_info(context, self.host, uuids)

    def _notify_about_instance_usage(self, context, instance, event_suffix, network_info=None, extra_usage_info=None, fault=None):
        compute_utils.notify_about_instance_usage(self.notifier, context, instance, event_suffix, network_info=network_info, extra_usage_info=extra_usage_info, fault=fault)

    def _deallocate_network(self, context, instance, requested_networks=None):
        if requested_networks and requested_networks.no_allocate:
            LOG.debug('Skipping network deallocation for instance since networking was not requested.', instance=instance)
            return
        LOG.debug('Deallocating network for instance', instance=instance)
        with timeutils.StopWatch() as timer:
            self.network_api.deallocate_for_instance(context, instance, requested_networks=requested_networks)
        LOG.info('Took %0.2f seconds to deallocate network for instance.', timer.elapsed(), instance=instance)

    def _get_instance_block_device_info(self, context, instance, refresh_conn_info=False, bdms=None):
        """Transform block devices to the driver block_device format."""
        if bdms is None:
            bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(context, instance.uuid)
        block_device_info = driver.get_block_device_info(instance, bdms)
        if not refresh_conn_info:
            block_device_info['block_device_mapping'] = [bdm for bdm in driver.block_device_info_get_mapping(block_device_info) if bdm.get('connection_info')]
        else:
            driver_block_device.refresh_conn_infos(driver.block_device_info_get_mapping(block_device_info), context, instance, self.volume_api, self.driver)
        self._block_device_info_to_legacy(block_device_info)
        return block_device_info

    def _build_failed(self, node):
        if CONF.compute.consecutive_build_service_disable_threshold:
            self.rt.build_failed(node)

    def _build_succeeded(self, node):
        self.rt.build_succeeded(node)

    @wrap_exception()
    @reverts_task_state
    @wrap_instance_fault
    def build_and_run_instance(self, context, instance, image, request_spec, filter_properties, accel_uuids, admin_password=None, injected_files=None, requested_networks=None, security_groups=None, block_device_mapping=None, node=None, limits=None, host_list=None):

        @utils.synchronized(instance.uuid)
        def _locked_do_build_and_run_instance(*args, **kwargs):
            with self._build_semaphore:
                try:
                    result = self._do_build_and_run_instance(*args, **kwargs)
                except Exception:
                    result = build_results.FAILED
                    raise
                finally:
                    if result == build_results.FAILED:
                        self.reportclient.delete_allocation_for_instance(context, instance.uuid)
                    if result in (build_results.FAILED, build_results.RESCHEDULED):
                        self._build_failed(node)
                    else:
                        self._build_succeeded(node)
        utils.spawn_n(_locked_do_build_and_run_instance, context, instance, image, request_spec, filter_properties, admin_password, injected_files, requested_networks, security_groups, block_device_mapping, node, limits, host_list, accel_uuids)

    def _check_device_tagging(self, requested_networks, block_device_mapping):
        tagging_requested = False
        if requested_networks:
            for net in requested_networks:
                if 'tag' in net and net.tag is not None:
                    tagging_requested = True
                    break
        if block_device_mapping and (not tagging_requested):
            for bdm in block_device_mapping:
                if 'tag' in bdm and bdm.tag is not None:
                    tagging_requested = True
                    break
        if tagging_requested and (not self.driver.capabilities.get('supports_device_tagging', False)):
            raise exception.BuildAbortException('Attempt to boot guest with tagged devices on host that does not support tagging.')

    def _check_trusted_certs(self, instance):
        if instance.trusted_certs and (not self.driver.capabilities.get('supports_trusted_certs', False)):
            raise exception.BuildAbortException('Trusted image certificates provided on host that does not support certificate validation.')

    @wrap_exception()
    @reverts_task_state
    @wrap_instance_event(prefix='compute')
    @wrap_instance_fault
    def _do_build_and_run_instance(self, context, instance, image, request_spec, filter_properties, admin_password, injected_files, requested_networks, security_groups, block_device_mapping, node=None, limits=None, host_list=None, accel_uuids=None):
        try:
            LOG.debug('Starting instance...', instance=instance)
            instance.vm_state = vm_states.BUILDING
            instance.task_state = None
            instance.save(expected_task_state=(task_states.SCHEDULING, None))
        except exception.InstanceNotFound:
            msg = 'Instance disappeared before build.'
            LOG.debug(msg, instance=instance)
            return build_results.FAILED
        except exception.UnexpectedTaskStateError as e:
            LOG.debug(e.format_message(), instance=instance)
            return build_results.FAILED
        decoded_files = self._decode_files(injected_files)
        if limits is None:
            limits = {}
        if node is None:
            node = self._get_nodename(instance, refresh=True)
        try:
            with timeutils.StopWatch() as timer:
                self._build_and_run_instance(context, instance, image, decoded_files, admin_password, requested_networks, security_groups, block_device_mapping, node, limits, filter_properties, request_spec, accel_uuids)
            LOG.info('Took %0.2f seconds to build instance.', timer.elapsed(), instance=instance)
            return build_results.ACTIVE
        except exception.RescheduledException as e:
            retry = filter_properties.get('retry')
            if not retry:
                LOG.debug('Retry info not present, will not reschedule', instance=instance)
                self._cleanup_allocated_networks(context, instance, requested_networks)
                compute_utils.add_instance_fault_from_exc(context, instance, e, sys.exc_info(), fault_message=e.kwargs['reason'])
                self._nil_out_instance_obj_host_and_node(instance)
                self._set_instance_obj_error_state(instance, clean_task_state=True)
                return build_results.FAILED
            LOG.debug(e.format_message(), instance=instance)
            retry['exc'] = traceback.format_exception(*sys.exc_info())
            retry['exc_reason'] = e.kwargs['reason']
            self._cleanup_allocated_networks(context, instance, requested_networks)
            self._nil_out_instance_obj_host_and_node(instance)
            instance.task_state = task_states.SCHEDULING
            instance.save()
            self.reportclient.delete_allocation_for_instance(context, instance.uuid)
            self.compute_task_api.build_instances(context, [instance], image, filter_properties, admin_password, injected_files, requested_networks, security_groups, block_device_mapping, request_spec=request_spec, host_lists=[host_list])
            return build_results.RESCHEDULED
        except (exception.InstanceNotFound, exception.UnexpectedDeletingTaskStateError):
            msg = 'Instance disappeared during build.'
            LOG.debug(msg, instance=instance)
            self._cleanup_allocated_networks(context, instance, requested_networks)
            return build_results.FAILED
        except Exception as e:
            if isinstance(e, exception.BuildAbortException):
                LOG.error(e.format_message(), instance=instance)
            else:
                LOG.exception('Unexpected build failure, not rescheduling build.', instance=instance)
            self._cleanup_allocated_networks(context, instance, requested_networks)
            self._cleanup_volumes(context, instance, block_device_mapping, raise_exc=False)
            compute_utils.add_instance_fault_from_exc(context, instance, e, sys.exc_info())
            self._nil_out_instance_obj_host_and_node(instance)
            self._set_instance_obj_error_state(instance, clean_task_state=True)
            return build_results.FAILED

    @staticmethod
    def _get_scheduler_hints(filter_properties, request_spec=None):
        """Helper method to get scheduler hints.

        This method prefers to get the hints out of the request spec, but that
        might not be provided. Conductor will pass request_spec down to the
        first compute chosen for a build but older computes will not pass
        the request_spec to conductor's build_instances method for a
        a reschedule, so if we're on a host via a retry, request_spec may not
        be provided so we need to fallback to use the filter_properties
        to get scheduler hints.
        """
        hints = {}
        if request_spec is not None and 'scheduler_hints' in request_spec:
            hints = request_spec.scheduler_hints
        if not hints:
            hints = filter_properties.get('scheduler_hints') or {}
        return hints

    @staticmethod
    def _get_request_group_mapping(request_spec):
        """Return request group resource - provider mapping. This is currently
        used for Neutron ports that have resource request due to the port
        having QoS minimum bandwidth policy rule attached.

        :param request_spec: A RequestSpec object or None
        :returns: A dict keyed by RequestGroup requester_id, currently Neutron
        port_id, to resource provider UUID that provides resource for that
        RequestGroup. Or None if the request_spec was None.
        """
        if request_spec:
            return request_spec.get_request_group_mapping()
        else:
            return None

    def _build_and_run_instance(self, context, instance, image, injected_files, admin_password, requested_networks, security_groups, block_device_mapping, node, limits, filter_properties, request_spec=None, accel_uuids=None):
        image_name = image.get('name')
        self._notify_about_instance_usage(context, instance, 'create.start', extra_usage_info={'image_name': image_name})
        compute_utils.notify_about_instance_create(context, instance, self.host, phase=fields.NotificationPhase.START, bdms=block_device_mapping)
        instance.system_metadata.update({'boot_roles': ','.join(context.roles)})
        self._check_device_tagging(requested_networks, block_device_mapping)
        self._check_trusted_certs(instance)
        provider_mapping = self._get_request_group_mapping(request_spec)
        if provider_mapping:
            try:
                compute_utils.update_pci_request_spec_with_allocated_interface_name(context, self.reportclient, instance.pci_requests.requests, provider_mapping)
            except (exception.AmbiguousResourceProviderForPCIRequest, exception.UnexpectedResourceProviderNameForPCIRequest) as e:
                raise exception.BuildAbortException(reason=str(e), instance_uuid=instance.uuid)
        allocs = self.reportclient.get_allocations_for_consumer(context, instance.uuid)
        try:
            scheduler_hints = self._get_scheduler_hints(filter_properties, request_spec)
            with self.rt.instance_claim(context, instance, node, allocs, limits):
                self._validate_instance_group_policy(context, instance, scheduler_hints)
                image_meta = objects.ImageMeta.from_dict(image)
                with self._build_resources(context, instance, requested_networks, security_groups, image_meta, block_device_mapping, provider_mapping, accel_uuids) as resources:
                    instance.vm_state = vm_states.BUILDING
                    instance.task_state = task_states.SPAWNING
                    instance.save(expected_task_state=task_states.BLOCK_DEVICE_MAPPING)
                    block_device_info = resources['block_device_info']
                    network_info = resources['network_info']
                    accel_info = resources['accel_info']
                    LOG.debug('Start spawning the instance on the hypervisor.', instance=instance)
                    with timeutils.StopWatch() as timer:
                        self.driver.spawn(context, instance, image_meta, injected_files, admin_password, allocs, network_info=network_info, block_device_info=block_device_info, accel_info=accel_info)
                    LOG.info('Took %0.2f seconds to spawn the instance on the hypervisor.', timer.elapsed(), instance=instance)
        except (exception.InstanceNotFound, exception.UnexpectedDeletingTaskStateError) as e:
            with excutils.save_and_reraise_exception():
                self._notify_about_instance_usage(context, instance, 'create.error', fault=e)
                compute_utils.notify_about_instance_create(context, instance, self.host, phase=fields.NotificationPhase.ERROR, exception=e, bdms=block_device_mapping)
        except exception.ComputeResourcesUnavailable as e:
            LOG.debug(e.format_message(), instance=instance)
            self._notify_about_instance_usage(context, instance, 'create.error', fault=e)
            compute_utils.notify_about_instance_create(context, instance, self.host, phase=fields.NotificationPhase.ERROR, exception=e, bdms=block_device_mapping)
            raise exception.RescheduledException(instance_uuid=instance.uuid, reason=e.format_message())
        except exception.BuildAbortException as e:
            with excutils.save_and_reraise_exception():
                LOG.debug(e.format_message(), instance=instance)
                self._notify_about_instance_usage(context, instance, 'create.error', fault=e)
                compute_utils.notify_about_instance_create(context, instance, self.host, phase=fields.NotificationPhase.ERROR, exception=e, bdms=block_device_mapping)
        except exception.NoMoreFixedIps as e:
            LOG.warning('No more fixed IP to be allocated', instance=instance)
            self._notify_about_instance_usage(context, instance, 'create.error', fault=e)
            compute_utils.notify_about_instance_create(context, instance, self.host, phase=fields.NotificationPhase.ERROR, exception=e, bdms=block_device_mapping)
            msg = _('Failed to allocate the network(s) with error %s, not rescheduling.') % e.format_message()
            raise exception.BuildAbortException(instance_uuid=instance.uuid, reason=msg)
        except (exception.ExternalNetworkAttachForbidden, exception.VirtualInterfaceCreateException, exception.VirtualInterfaceMacAddressException, exception.FixedIpInvalidOnHost, exception.UnableToAutoAllocateNetwork, exception.NetworksWithQoSPolicyNotSupported) as e:
            LOG.exception('Failed to allocate network(s)', instance=instance)
            self._notify_about_instance_usage(context, instance, 'create.error', fault=e)
            compute_utils.notify_about_instance_create(context, instance, self.host, phase=fields.NotificationPhase.ERROR, exception=e, bdms=block_device_mapping)
            msg = _('Failed to allocate the network(s), not rescheduling.')
            raise exception.BuildAbortException(instance_uuid=instance.uuid, reason=msg)
        except (exception.FlavorDiskTooSmall, exception.FlavorMemoryTooSmall, exception.ImageNotActive, exception.ImageUnacceptable, exception.InvalidDiskInfo, exception.InvalidDiskFormat, cursive_exception.SignatureVerificationError, exception.CertificateValidationFailed, exception.VolumeEncryptionNotSupported, exception.InvalidInput, exception.RequestedVRamTooHigh) as e:
            self._notify_about_instance_usage(context, instance, 'create.error', fault=e)
            compute_utils.notify_about_instance_create(context, instance, self.host, phase=fields.NotificationPhase.ERROR, exception=e, bdms=block_device_mapping)
            raise exception.BuildAbortException(instance_uuid=instance.uuid, reason=e.format_message())
        except Exception as e:
            LOG.exception('Failed to build and run instance', instance=instance)
            self._notify_about_instance_usage(context, instance, 'create.error', fault=e)
            compute_utils.notify_about_instance_create(context, instance, self.host, phase=fields.NotificationPhase.ERROR, exception=e, bdms=block_device_mapping)
            raise exception.RescheduledException(instance_uuid=instance.uuid, reason=str(e))
        instance.system_metadata.pop('network_allocated', None)
        network_name = CONF.default_access_ip_network_name
        if network_name and (not instance.access_ip_v4) and (not instance.access_ip_v6):
            for vif in network_info:
                if vif['network']['label'] == network_name:
                    for ip in vif.fixed_ips():
                        if not instance.access_ip_v4 and ip['version'] == 4:
                            instance.access_ip_v4 = ip['address']
                        if not instance.access_ip_v6 and ip['version'] == 6:
                            instance.access_ip_v6 = ip['address']
                    break
        self._update_instance_after_spawn(instance)
        try:
            instance.save(expected_task_state=task_states.SPAWNING)
        except (exception.InstanceNotFound, exception.UnexpectedDeletingTaskStateError) as e:
            with excutils.save_and_reraise_exception():
                self._notify_about_instance_usage(context, instance, 'create.error', fault=e)
                compute_utils.notify_about_instance_create(context, instance, self.host, phase=fields.NotificationPhase.ERROR, exception=e, bdms=block_device_mapping)
        self._update_scheduler_instance_info(context, instance)
        self._notify_about_instance_usage(context, instance, 'create.end', extra_usage_info={'message': _('Success')}, network_info=network_info)
        compute_utils.notify_about_instance_create(context, instance, self.host, phase=fields.NotificationPhase.END, bdms=block_device_mapping)

    def _build_resources_cleanup(self, instance, network_info):
        if network_info is not None:
            network_info.wait(do_raise=False)
            self.driver.clean_networks_preparation(instance, network_info)
        self.driver.failed_spawn_cleanup(instance)

    @contextlib.contextmanager
    def _build_resources(self, context, instance, requested_networks, security_groups, image_meta, block_device_mapping, resource_provider_mapping, accel_uuids):
        resources = {}
        network_info = None
        try:
            LOG.debug('Start building networks asynchronously for instance.', instance=instance)
            network_info = self._build_networks_for_instance(context, instance, requested_networks, security_groups, resource_provider_mapping)
            resources['network_info'] = network_info
        except (exception.InstanceNotFound, exception.UnexpectedDeletingTaskStateError):
            raise
        except exception.UnexpectedTaskStateError as e:
            raise exception.BuildAbortException(instance_uuid=instance.uuid, reason=e.format_message())
        except Exception:
            LOG.exception('Failed to allocate network(s)', instance=instance)
            msg = _('Failed to allocate the network(s), not rescheduling.')
            raise exception.BuildAbortException(instance_uuid=instance.uuid, reason=msg)
        try:
            self.driver.prepare_for_spawn(instance)
            self.driver.prepare_networks_before_block_device_mapping(instance, network_info)
            self._default_block_device_names(instance, image_meta, block_device_mapping)
            LOG.debug('Start building block device mappings for instance.', instance=instance)
            instance.vm_state = vm_states.BUILDING
            instance.task_state = task_states.BLOCK_DEVICE_MAPPING
            instance.save()
            block_device_info = self._prep_block_device(context, instance, block_device_mapping)
            resources['block_device_info'] = block_device_info
        except (exception.InstanceNotFound, exception.UnexpectedDeletingTaskStateError):
            with excutils.save_and_reraise_exception():
                self._build_resources_cleanup(instance, network_info)
        except (exception.UnexpectedTaskStateError, exception.OverQuota, exception.InvalidBDM) as e:
            self._build_resources_cleanup(instance, network_info)
            raise exception.BuildAbortException(instance_uuid=instance.uuid, reason=e.format_message())
        except Exception:
            LOG.exception('Failure prepping block device', instance=instance)
            self._build_resources_cleanup(instance, network_info)
            msg = _('Failure prepping block device.')
            raise exception.BuildAbortException(instance_uuid=instance.uuid, reason=msg)
        arqs = []
        if instance.flavor.extra_specs.get('accel:device_profile'):
            try:
                arqs = self._get_bound_arq_resources(context, instance, accel_uuids)
            except (Exception, eventlet.timeout.Timeout) as exc:
                LOG.exception(exc)
                self._build_resources_cleanup(instance, network_info)
                compute_utils.delete_arqs_if_needed(context, instance)
                msg = _('Failure getting accelerator requests.')
                raise exception.BuildAbortException(reason=msg, instance_uuid=instance.uuid)
        resources['accel_info'] = arqs
        try:
            yield resources
        except Exception as exc:
            with excutils.save_and_reraise_exception() as ctxt:
                if not isinstance(exc, (exception.InstanceNotFound, exception.UnexpectedDeletingTaskStateError)):
                    LOG.exception('Instance failed to spawn', instance=instance)
                if network_info is not None:
                    network_info.wait(do_raise=False)
                deallocate_networks = False if network_info else True
                try:
                    self._shutdown_instance(context, instance, block_device_mapping, requested_networks, try_deallocate_networks=deallocate_networks)
                except Exception as exc2:
                    ctxt.reraise = False
                    LOG.warning('Could not clean up failed build, not rescheduling. Error: %s', str(exc2))
                    raise exception.BuildAbortException(instance_uuid=instance.uuid, reason=str(exc))
                finally:
                    compute_utils.delete_arqs_if_needed(context, instance)

    def _get_bound_arq_resources(self, context, instance, arq_uuids):
        """Get bound accelerator requests.

        The ARQ binding was kicked off in the conductor as an async
        operation. Here we wait for the notification from Cyborg.

        If the notification arrived before this point, which can happen
        in many/most cases (see [1]), it will be lost. To handle that,
        we use exit_wait_early.
        [1] https://review.opendev.org/#/c/631244/46/nova/compute/
            manager.py@2627

        :param instance: instance object
        :param arq_uuids: List of accelerator request (ARQ) UUIDs.
        :returns: List of ARQs for which bindings have completed,
                  successfully or otherwise
        """
        cyclient = cyborg.get_client(context)
        if arq_uuids is None:
            arqs = cyclient.get_arqs_for_instance(instance.uuid)
            arq_uuids = [arq['uuid'] for arq in arqs]
        events = [('accelerator-request-bound', arq_uuid) for arq_uuid in arq_uuids]
        timeout = CONF.arq_binding_timeout
        with self.virtapi.wait_for_instance_event(instance, events, deadline=timeout):
            resolved_arqs = cyclient.get_arqs_for_instance(instance.uuid, only_resolved=True)
            early_events = [('accelerator-request-bound', arq['uuid']) for arq in resolved_arqs]
            if early_events:
                self.virtapi.exit_wait_early(early_events)
        resolved_uuids = [arq['uuid'] for arq in resolved_arqs]
        if sorted(resolved_uuids) != sorted(arq_uuids):
            arqs = cyclient.get_arqs_for_instance(instance.uuid)
        else:
            arqs = resolved_arqs
        return arqs

    def _cleanup_allocated_networks(self, context, instance, requested_networks):
        """Cleanup networks allocated for instance.

        :param context: nova request context
        :param instance: nova.objects.instance.Instance object
        :param requested_networks: nova.objects.NetworkRequestList
        """
        LOG.debug('Unplugging VIFs for instance', instance=instance)
        network_info = instance.get_network_info()
        if not network_info:
            try:
                network_info = self.network_api.get_instance_nw_info(context, instance)
            except Exception as exc:
                LOG.warning('Failed to update network info cache when cleaning up allocated networks. Stale VIFs may be left on this host.Error: %s', str(exc))
                return
        try:
            self.driver.unplug_vifs(instance, network_info)
        except NotImplementedError:
            LOG.debug('Virt driver does not provide unplug_vifs method, so it is not possible determine if VIFs should be unplugged.')
        except exception.NovaException as exc:
            LOG.warning('Cleaning up VIFs failed for instance. Error: %s', str(exc), instance=instance)
        else:
            LOG.debug('Unplugged VIFs for instance', instance=instance)
        try:
            self._deallocate_network(context, instance, requested_networks)
        except Exception:
            LOG.exception('Failed to deallocate networks', instance=instance)
            return
        instance.system_metadata['network_allocated'] = 'False'
        try:
            instance.save()
        except exception.InstanceNotFound:
            pass

    def _try_deallocate_network(self, context, instance, requested_networks=None):

        @loopingcall.RetryDecorator(max_retry_count=3, inc_sleep_time=2, max_sleep_time=12, exceptions=(keystone_exception.connection.ConnectFailure,))
        def _deallocate_network_with_retries():
            try:
                self._deallocate_network(context, instance, requested_networks)
            except keystone_exception.connection.ConnectFailure as e:
                with excutils.save_and_reraise_exception():
                    LOG.warning('Failed to deallocate network for instance; retrying. Error: %s', str(e), instance=instance)
        try:
            _deallocate_network_with_retries()
        except Exception as ex:
            with excutils.save_and_reraise_exception():
                LOG.error('Failed to deallocate network for instance. Error: %s', ex, instance=instance)
                self._set_instance_obj_error_state(instance)

    def _get_power_off_values(self, instance, clean_shutdown):
        """Get the timing configuration for powering down this instance."""
        if clean_shutdown:
            timeout = compute_utils.get_value_from_system_metadata(instance, key='image_os_shutdown_timeout', type=int, default=CONF.shutdown_timeout)
            retry_interval = CONF.compute.shutdown_retry_interval
        else:
            timeout = 0
            retry_interval = 0
        return (timeout, retry_interval)

    def _power_off_instance(self, instance, clean_shutdown=True):
        """Power off an instance on this host."""
        (timeout, retry_interval) = self._get_power_off_values(instance, clean_shutdown)
        self.driver.power_off(instance, timeout, retry_interval)

    def _shutdown_instance(self, context, instance, bdms, requested_networks=None, notify=True, try_deallocate_networks=True):
        """Shutdown an instance on this host.

        :param:context: security context
        :param:instance: a nova.objects.Instance object
        :param:bdms: the block devices for the instance to be torn
                     down
        :param:requested_networks: the networks on which the instance
                                   has ports
        :param:notify: true if a final usage notification should be
                       emitted
        :param:try_deallocate_networks: false if we should avoid
                                        trying to teardown networking
        """
        context = context.elevated()
        LOG.info('Terminating instance', instance=instance)
        if notify:
            self._notify_about_instance_usage(context, instance, 'shutdown.start')
            compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.SHUTDOWN, phase=fields.NotificationPhase.START, bdms=bdms)
        network_info = instance.get_network_info()
        if not network_info:
            network_info = self.network_api.get_instance_nw_info(context, instance)
        vol_bdms = [bdm for bdm in bdms if bdm.is_volume]
        block_device_info = self._get_instance_block_device_info(context, instance, bdms=bdms)
        try:
            LOG.debug('Start destroying the instance on the hypervisor.', instance=instance)
            with timeutils.StopWatch() as timer:
                self.driver.destroy(context, instance, network_info, block_device_info)
            LOG.info('Took %0.2f seconds to destroy the instance on the hypervisor.', timer.elapsed(), instance=instance)
        except exception.InstancePowerOffFailure:
            with excutils.save_and_reraise_exception():
                pass
        except Exception:
            with excutils.save_and_reraise_exception():
                if try_deallocate_networks:
                    self._try_deallocate_network(context, instance, requested_networks)
        if try_deallocate_networks:
            self._try_deallocate_network(context, instance, requested_networks)
        timer.restart()
        for bdm in vol_bdms:
            try:
                if bdm.attachment_id:
                    self.volume_api.attachment_delete(context, bdm.attachment_id)
                else:
                    connector = self.driver.get_volume_connector(instance)
                    self.volume_api.terminate_connection(context, bdm.volume_id, connector)
                    self.volume_api.detach(context, bdm.volume_id, instance.uuid)
            except exception.VolumeAttachmentNotFound as exc:
                LOG.debug('Ignoring VolumeAttachmentNotFound: %s', exc, instance=instance)
            except exception.DiskNotFound as exc:
                LOG.debug('Ignoring DiskNotFound: %s', exc, instance=instance)
            except exception.VolumeNotFound as exc:
                LOG.debug('Ignoring VolumeNotFound: %s', exc, instance=instance)
            except (cinder_exception.EndpointNotFound, keystone_exception.EndpointNotFound) as exc:
                LOG.warning('Ignoring EndpointNotFound for volume %(volume_id)s: %(exc)s', {'exc': exc, 'volume_id': bdm.volume_id}, instance=instance)
            except cinder_exception.ClientException as exc:
                LOG.warning('Ignoring unknown cinder exception for volume %(volume_id)s: %(exc)s', {'exc': exc, 'volume_id': bdm.volume_id}, instance=instance)
            except Exception as exc:
                LOG.warning('Ignoring unknown exception for volume %(volume_id)s: %(exc)s', {'exc': exc, 'volume_id': bdm.volume_id}, instance=instance)
        if vol_bdms:
            LOG.info('Took %(time).2f seconds to detach %(num)s volumes for instance.', {'time': timer.elapsed(), 'num': len(vol_bdms)}, instance=instance)
        if notify:
            self._notify_about_instance_usage(context, instance, 'shutdown.end')
            compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.SHUTDOWN, phase=fields.NotificationPhase.END, bdms=bdms)

    def _cleanup_volumes(self, context, instance, bdms, raise_exc=True, detach=True):
        original_exception = None
        for bdm in bdms:
            if detach and bdm.volume_id:
                try:
                    LOG.debug('Detaching volume: %s', bdm.volume_id, instance_uuid=instance.uuid)
                    destroy = bdm.delete_on_termination
                    self._detach_volume(context, bdm, instance, destroy_bdm=destroy)
                except Exception as exc:
                    original_exception = exc
                    LOG.warning('Failed to detach volume: %(volume_id)s due to %(exc)s', {'volume_id': bdm.volume_id, 'exc': exc})
            if bdm.volume_id and bdm.delete_on_termination:
                try:
                    LOG.debug('Deleting volume: %s', bdm.volume_id, instance_uuid=instance.uuid)
                    self.volume_api.delete(context, bdm.volume_id)
                except Exception as exc:
                    original_exception = exc
                    LOG.warning('Failed to delete volume: %(volume_id)s due to %(exc)s', {'volume_id': bdm.volume_id, 'exc': exc})
        if original_exception is not None and raise_exc:
            raise original_exception

    def _delete_instance(self, context, instance, bdms):
        """Delete an instance on this host.

        :param context: nova request context
        :param instance: nova.objects.instance.Instance object
        :param bdms: nova.objects.block_device.BlockDeviceMappingList object
        """
        events = self.instance_events.clear_events_for_instance(instance)
        if events:
            LOG.debug('Events pending at deletion: %(events)s', {'events': ','.join(events.keys())}, instance=instance)
        self._notify_about_instance_usage(context, instance, 'delete.start')
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.DELETE, phase=fields.NotificationPhase.START, bdms=bdms)
        self._shutdown_instance(context, instance, bdms)
        self._cleanup_volumes(context, instance, bdms, raise_exc=False, detach=False)
        compute_utils.delete_arqs_if_needed(context, instance)
        instance.vm_state = vm_states.DELETED
        instance.task_state = None
        instance.power_state = power_state.NOSTATE
        instance.terminated_at = timeutils.utcnow()
        instance.save()
        self._complete_deletion(context, instance)
        instance.destroy()
        self._notify_about_instance_usage(context, instance, 'delete.end')
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.DELETE, phase=fields.NotificationPhase.END, bdms=bdms)

    @wrap_exception()
    @reverts_task_state
    @wrap_instance_event(prefix='compute')
    @wrap_instance_fault
    def terminate_instance(self, context, instance, bdms):
        """Terminate an instance on this host."""

        @utils.synchronized(instance.uuid)
        def do_terminate_instance(instance, bdms):
            for bdm in list(bdms):
                if bdm.is_volume and (not bdm.volume_id):
                    LOG.debug('There are potentially stale BDMs during delete, refreshing the BlockDeviceMappingList.', instance=instance)
                    bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(context, instance.uuid)
                    break
            try:
                self._delete_instance(context, instance, bdms)
            except exception.InstanceNotFound:
                LOG.info('Instance disappeared during terminate', instance=instance)
            except Exception:
                with excutils.save_and_reraise_exception():
                    LOG.exception('Setting instance vm_state to ERROR', instance=instance)
                    self._set_instance_obj_error_state(instance)
        do_terminate_instance(instance, bdms)

    @wrap_exception()
    @reverts_task_state
    @wrap_instance_event(prefix='compute')
    @wrap_instance_fault
    def stop_instance(self, context, instance, clean_shutdown):
        """Stopping an instance on this host."""

        @utils.synchronized(instance.uuid)
        def do_stop_instance():
            current_power_state = self._get_power_state(instance)
            LOG.debug('Stopping instance; current vm_state: %(vm_state)s, current task_state: %(task_state)s, current DB power_state: %(db_power_state)s, current VM power_state: %(current_power_state)s', {'vm_state': instance.vm_state, 'task_state': instance.task_state, 'db_power_state': instance.power_state, 'current_power_state': current_power_state}, instance_uuid=instance.uuid)
            expected_task_state = [task_states.POWERING_OFF]
            if current_power_state in (power_state.NOSTATE, power_state.SHUTDOWN, power_state.CRASHED):
                LOG.info('Instance is already powered off in the hypervisor when stop is called.', instance=instance)
                expected_task_state.append(None)
            self._notify_about_instance_usage(context, instance, 'power_off.start')
            compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.POWER_OFF, phase=fields.NotificationPhase.START)
            self._power_off_instance(instance, clean_shutdown)
            instance.power_state = self._get_power_state(instance)
            instance.vm_state = vm_states.STOPPED
            instance.task_state = None
            instance.save(expected_task_state=expected_task_state)
            self._notify_about_instance_usage(context, instance, 'power_off.end')
            compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.POWER_OFF, phase=fields.NotificationPhase.END)
        do_stop_instance()

    def _power_on(self, context, instance):
        network_info = self.network_api.get_instance_nw_info(context, instance)
        block_device_info = self._get_instance_block_device_info(context, instance)
        accel_info = self._get_accel_info(context, instance)
        self.driver.power_on(context, instance, network_info, block_device_info, accel_info)

    def _delete_snapshot_of_shelved_instance(self, context, instance, snapshot_id):
        """Delete snapshot of shelved instance."""
        try:
            self.image_api.delete(context, snapshot_id)
        except (exception.ImageNotFound, exception.ImageNotAuthorized) as exc:
            LOG.warning('Failed to delete snapshot from shelved instance (%s).', exc.format_message(), instance=instance)
        except Exception:
            LOG.exception('Something wrong happened when trying to delete snapshot from shelved instance.', instance=instance)

    @wrap_exception()
    @reverts_task_state
    @wrap_instance_event(prefix='compute')
    @wrap_instance_fault
    def start_instance(self, context, instance):
        """Starting an instance on this host."""
        self._notify_about_instance_usage(context, instance, 'power_on.start')
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.POWER_ON, phase=fields.NotificationPhase.START)
        self._power_on(context, instance)
        instance.power_state = self._get_power_state(instance)
        instance.vm_state = vm_states.ACTIVE
        instance.task_state = None
        snapshot_id = instance.system_metadata.get('shelved_image_id')
        if snapshot_id:
            self._delete_snapshot_of_shelved_instance(context, instance, snapshot_id)
        compute_utils.remove_shelved_keys_from_system_metadata(instance)
        instance.save(expected_task_state=task_states.POWERING_ON)
        self._notify_about_instance_usage(context, instance, 'power_on.end')
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.POWER_ON, phase=fields.NotificationPhase.END)

    @messaging.expected_exceptions(NotImplementedError, exception.TriggerCrashDumpNotSupported, exception.InstanceNotRunning)
    @wrap_exception()
    @wrap_instance_event(prefix='compute')
    @wrap_instance_fault
    def trigger_crash_dump(self, context, instance):
        """Trigger crash dump in an instance."""
        self._notify_about_instance_usage(context, instance, 'trigger_crash_dump.start')
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.TRIGGER_CRASH_DUMP, phase=fields.NotificationPhase.START)
        self.driver.trigger_crash_dump(instance)
        self._notify_about_instance_usage(context, instance, 'trigger_crash_dump.end')
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.TRIGGER_CRASH_DUMP, phase=fields.NotificationPhase.END)

    @wrap_exception()
    @reverts_task_state
    @wrap_instance_event(prefix='compute')
    @wrap_instance_fault
    def soft_delete_instance(self, context, instance):
        """Soft delete an instance on this host."""
        with compute_utils.notify_about_instance_delete(self.notifier, context, instance, 'soft_delete', source=fields.NotificationSource.COMPUTE):
            try:
                self.driver.soft_delete(instance)
            except NotImplementedError:
                self.driver.power_off(instance)
            instance.power_state = self._get_power_state(instance)
            instance.vm_state = vm_states.SOFT_DELETED
            instance.task_state = None
            instance.save(expected_task_state=[task_states.SOFT_DELETING])

    @wrap_exception()
    @reverts_task_state
    @wrap_instance_event(prefix='compute')
    @wrap_instance_fault
    def restore_instance(self, context, instance):
        """Restore a soft-deleted instance on this host."""
        self._notify_about_instance_usage(context, instance, 'restore.start')
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.RESTORE, phase=fields.NotificationPhase.START)
        try:
            self.driver.restore(instance)
        except NotImplementedError:
            self._power_on(context, instance)
        instance.power_state = self._get_power_state(instance)
        instance.vm_state = vm_states.ACTIVE
        instance.task_state = None
        instance.save(expected_task_state=task_states.RESTORING)
        self._notify_about_instance_usage(context, instance, 'restore.end')
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.RESTORE, phase=fields.NotificationPhase.END)

    @staticmethod
    def _set_migration_status(migration, status):
        """Set the status, and guard against a None being passed in.

        This is useful as some of the compute RPC calls will not pass
        a migration object in older versions. The check can be removed when
        we move past 4.x major version of the RPC API.
        """
        if migration:
            migration.status = status
            migration.save()

    def _rebuild_default_impl(self, context, instance, image_meta, injected_files, admin_password, allocations, bdms, detach_block_devices, attach_block_devices, network_info=None, evacuate=False, block_device_info=None, preserve_ephemeral=False, accel_uuids=None):
        if preserve_ephemeral:
            raise exception.PreserveEphemeralNotSupported()
        accel_info = []
        if evacuate:
            if instance.flavor.extra_specs.get('accel:device_profile'):
                try:
                    accel_info = self._get_bound_arq_resources(context, instance, accel_uuids or [])
                except (Exception, eventlet.timeout.Timeout) as exc:
                    LOG.exception(exc)
                    self._build_resources_cleanup(instance, network_info)
                    msg = _('Failure getting accelerator resources.')
                    raise exception.BuildAbortException(instance_uuid=instance.uuid, reason=msg)
            detach_block_devices(context, bdms)
        else:
            self._power_off_instance(instance, clean_shutdown=True)
            detach_block_devices(context, bdms)
            self.driver.destroy(context, instance, network_info=network_info, block_device_info=block_device_info)
            try:
                accel_info = self._get_accel_info(context, instance)
            except Exception as exc:
                LOG.exception(exc)
                self._build_resources_cleanup(instance, network_info)
                msg = _('Failure getting accelerator resources.')
                raise exception.BuildAbortException(instance_uuid=instance.uuid, reason=msg)
        instance.task_state = task_states.REBUILD_BLOCK_DEVICE_MAPPING
        instance.save(expected_task_state=[task_states.REBUILDING])
        new_block_device_info = attach_block_devices(context, instance, bdms)
        instance.task_state = task_states.REBUILD_SPAWNING
        instance.save(expected_task_state=[task_states.REBUILD_BLOCK_DEVICE_MAPPING])
        with instance.mutated_migration_context():
            self.driver.spawn(context, instance, image_meta, injected_files, admin_password, allocations, network_info=network_info, block_device_info=new_block_device_info, accel_info=accel_info)

    def _notify_instance_rebuild_error(self, context, instance, error, bdms):
        self._notify_about_instance_usage(context, instance, 'rebuild.error', fault=error)
        compute_utils.notify_about_instance_rebuild(context, instance, self.host, phase=fields.NotificationPhase.ERROR, exception=error, bdms=bdms)

    @messaging.expected_exceptions(exception.PreserveEphemeralNotSupported, exception.BuildAbortException)
    @wrap_exception()
    @reverts_task_state
    @wrap_instance_event(prefix='compute')
    @wrap_instance_fault
    def rebuild_instance(self, context, instance, orig_image_ref, image_ref, injected_files, new_pass, orig_sys_metadata, bdms, recreate, on_shared_storage, preserve_ephemeral, migration, scheduled_node, limits, request_spec, accel_uuids):
        """Destroy and re-make this instance.

        A 'rebuild' effectively purges all existing data from the system and
        remakes the VM with given 'metadata' and 'personalities'.

        :param context: `nova.RequestContext` object
        :param instance: Instance object
        :param orig_image_ref: Original image_ref before rebuild
        :param image_ref: New image_ref for rebuild
        :param injected_files: Files to inject
        :param new_pass: password to set on rebuilt instance
        :param orig_sys_metadata: instance system metadata from pre-rebuild
        :param bdms: block-device-mappings to use for rebuild
        :param recreate: True if the instance is being evacuated (e.g. the
            hypervisor it was on failed) - cleanup of old state will be
            skipped.
        :param on_shared_storage: True if instance files on shared storage.
                                  If not provided then information from the
                                  driver will be used to decide if the instance
                                  files are available or not on the target host
        :param preserve_ephemeral: True if the default ephemeral storage
                                   partition must be preserved on rebuild
        :param migration: a Migration object if one was created for this
                          rebuild operation (if it's a part of evacuate)
        :param scheduled_node: A node of the host chosen by the scheduler. If a
                               host was specified by the user, this will be
                               None
        :param limits: Overcommit limits set by the scheduler. If a host was
                       specified by the user, this will be None
        :param request_spec: a RequestSpec object used to schedule the instance
        :param accel_uuids: a list of cyborg ARQ uuids

        """
        evacuate = recreate
        context = context.elevated()
        if evacuate:
            LOG.info('Evacuating instance', instance=instance)
        else:
            LOG.info('Rebuilding instance', instance=instance)
        if evacuate:
            rebuild_claim = self.rt.rebuild_claim
        else:
            rebuild_claim = claims.NopClaim
        if image_ref:
            image_meta = objects.ImageMeta.from_image_ref(context, self.image_api, image_ref)
        elif evacuate:
            image_meta = instance.image_meta
        else:
            image_meta = objects.ImageMeta()
        if not scheduled_node:
            if evacuate:
                try:
                    compute_node = self._get_compute_info(context, self.host)
                    scheduled_node = compute_node.hypervisor_hostname
                except exception.ComputeHostNotFound:
                    LOG.exception('Failed to get compute_info for %s', self.host)
            else:
                scheduled_node = instance.node
        allocs = self.reportclient.get_allocations_for_consumer(context, instance.uuid)
        instance_state = instance.vm_state
        with self._error_out_instance_on_exception(context, instance, instance_state=instance_state):
            try:
                self._do_rebuild_instance_with_claim(context, instance, orig_image_ref, image_meta, injected_files, new_pass, orig_sys_metadata, bdms, evacuate, on_shared_storage, preserve_ephemeral, migration, request_spec, allocs, rebuild_claim, scheduled_node, limits, accel_uuids)
            except (exception.ComputeResourcesUnavailable, exception.RescheduledException) as e:
                if isinstance(e, exception.ComputeResourcesUnavailable):
                    LOG.debug('Could not rebuild instance on this host, not enough resources available.', instance=instance)
                else:
                    LOG.debug('Could not rebuild instance on this host, late server group check failed.', instance=instance)
                self._set_migration_status(migration, 'failed')
                self.rt.delete_allocation_for_evacuated_instance(context, instance, scheduled_node, node_type='destination')
                self._notify_instance_rebuild_error(context, instance, e, bdms)
                raise exception.InstanceFaultRollback(inner_exception=exception.BuildAbortException(instance_uuid=instance.uuid, reason=e.format_message()))
            except (exception.InstanceNotFound, exception.UnexpectedDeletingTaskStateError) as e:
                LOG.debug('Instance was deleted while rebuilding', instance=instance)
                self._set_migration_status(migration, 'failed')
                self._notify_instance_rebuild_error(context, instance, e, bdms)
            except Exception as e:
                self._set_migration_status(migration, 'failed')
                if evacuate or scheduled_node is not None:
                    self.rt.delete_allocation_for_evacuated_instance(context, instance, scheduled_node, node_type='destination')
                self._notify_instance_rebuild_error(context, instance, e, bdms)
                raise
            else:
                self.rt.finish_evacuation(instance, scheduled_node, migration)

    def _do_rebuild_instance_with_claim(self, context, instance, orig_image_ref, image_meta, injected_files, new_pass, orig_sys_metadata, bdms, evacuate, on_shared_storage, preserve_ephemeral, migration, request_spec, allocations, rebuild_claim, scheduled_node, limits, accel_uuids):
        """Helper to avoid deep nesting in the top-level method."""
        provider_mapping = None
        if evacuate:
            provider_mapping = self._get_request_group_mapping(request_spec)
            if provider_mapping:
                compute_utils.update_pci_request_spec_with_allocated_interface_name(context, self.reportclient, instance.pci_requests.requests, provider_mapping)
        claim_context = rebuild_claim(context, instance, scheduled_node, allocations, limits=limits, image_meta=image_meta, migration=migration)
        with claim_context:
            self._do_rebuild_instance(context, instance, orig_image_ref, image_meta, injected_files, new_pass, orig_sys_metadata, bdms, evacuate, on_shared_storage, preserve_ephemeral, migration, request_spec, allocations, provider_mapping, accel_uuids)

    @staticmethod
    def _get_image_name(image_meta):
        if image_meta.obj_attr_is_set('name'):
            return image_meta.name
        else:
            return ''

    def _do_rebuild_instance(self, context, instance, orig_image_ref, image_meta, injected_files, new_pass, orig_sys_metadata, bdms, evacuate, on_shared_storage, preserve_ephemeral, migration, request_spec, allocations, request_group_resource_providers_mapping, accel_uuids):
        orig_vm_state = instance.vm_state
        if evacuate:
            if request_spec:
                hints = self._get_scheduler_hints({}, request_spec)
                self._validate_instance_group_policy(context, instance, hints)
            if not self.driver.capabilities.get('supports_evacuate', False):
                raise exception.InstanceEvacuateNotSupported
            self._check_instance_exists(instance)
            if on_shared_storage is None:
                LOG.debug('on_shared_storage is not provided, using driver information to decide if the instance needs to be evacuated')
                on_shared_storage = self.driver.instance_on_disk(instance)
            elif on_shared_storage != self.driver.instance_on_disk(instance):
                raise exception.InvalidSharedStorage(_('Invalid state of instance files on shared storage'))
            if on_shared_storage:
                LOG.info('disk on shared storage, evacuating using existing disk')
            elif instance.image_ref:
                orig_image_ref = instance.image_ref
                LOG.info("disk not on shared storage, evacuating from image: '%s'", str(orig_image_ref))
            else:
                LOG.info('disk on volume, evacuating using existing volume')
        self._check_trusted_certs(instance)
        orig_image_ref_url = self.image_api.generate_image_url(orig_image_ref, context)
        extra_usage_info = {'image_ref_url': orig_image_ref_url}
        compute_utils.notify_usage_exists(self.notifier, context, instance, self.host, current_period=True, system_metadata=orig_sys_metadata, extra_usage_info=extra_usage_info)
        extra_usage_info = {'image_name': self._get_image_name(image_meta)}
        self._notify_about_instance_usage(context, instance, 'rebuild.start', extra_usage_info=extra_usage_info)
        compute_utils.notify_about_instance_rebuild(context, instance, self.host, phase=fields.NotificationPhase.START, bdms=bdms)
        instance.power_state = self._get_power_state(instance)
        instance.task_state = task_states.REBUILDING
        instance.save(expected_task_state=[task_states.REBUILDING])
        if evacuate:
            self.network_api.setup_networks_on_host(context, instance, self.host)
            self.network_api.setup_instance_network_on_host(context, instance, self.host, migration, provider_mappings=request_group_resource_providers_mapping)
            network_info = self.network_api.get_instance_nw_info(context, instance)
        else:
            network_info = instance.get_network_info()
        if bdms is None:
            bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(context, instance.uuid)
        block_device_info = self._get_instance_block_device_info(context, instance, bdms=bdms)

        def detach_block_devices(context, bdms):
            for bdm in bdms:
                if bdm.is_volume:
                    attachment_id = None
                    if bdm.attachment_id:
                        attachment_id = self.volume_api.attachment_create(context, bdm['volume_id'], instance.uuid)['id']
                    self._detach_volume(context, bdm, instance, destroy_bdm=False)
                    if attachment_id:
                        bdm.attachment_id = attachment_id
                        bdm.save()
        files = self._decode_files(injected_files)
        kwargs = dict(context=context, instance=instance, image_meta=image_meta, injected_files=files, admin_password=new_pass, allocations=allocations, bdms=bdms, detach_block_devices=detach_block_devices, attach_block_devices=self._prep_block_device, block_device_info=block_device_info, network_info=network_info, preserve_ephemeral=preserve_ephemeral, evacuate=evacuate, accel_uuids=accel_uuids)
        try:
            with instance.mutated_migration_context():
                self.driver.rebuild(**kwargs)
        except NotImplementedError:
            self._rebuild_default_impl(**kwargs)
        self._update_instance_after_spawn(instance)
        instance.save(expected_task_state=[task_states.REBUILD_SPAWNING])
        if orig_vm_state == vm_states.STOPPED:
            LOG.info("bringing vm to original state: '%s'", orig_vm_state, instance=instance)
            instance.vm_state = vm_states.ACTIVE
            instance.task_state = task_states.POWERING_OFF
            instance.progress = 0
            instance.save()
            self.stop_instance(context, instance, False)
        self._update_scheduler_instance_info(context, instance)
        self._notify_about_instance_usage(context, instance, 'rebuild.end', network_info=network_info, extra_usage_info=extra_usage_info)
        compute_utils.notify_about_instance_rebuild(context, instance, self.host, phase=fields.NotificationPhase.END, bdms=bdms)

    def _handle_bad_volumes_detached(self, context, instance, bad_devices, block_device_info):
        """Handle cases where the virt-layer had to detach non-working volumes
        in order to complete an operation.
        """
        for bdm in block_device_info['block_device_mapping']:
            if bdm.get('mount_device') in bad_devices:
                try:
                    volume_id = bdm['connection_info']['data']['volume_id']
                except KeyError:
                    continue
                LOG.info('Detaching from volume api: %s', volume_id)
                self.volume_api.begin_detaching(context, volume_id)
                self.detach_volume(context, volume_id, instance)

    def _get_accel_info(self, context, instance):
        dp_name = instance.flavor.extra_specs.get('accel:device_profile')
        if dp_name:
            cyclient = cyborg.get_client(context)
            accel_info = cyclient.get_arqs_for_instance(instance.uuid)
        else:
            accel_info = []
        return accel_info

    @wrap_exception()
    @reverts_task_state
    @wrap_instance_event(prefix='compute')
    @wrap_instance_fault
    def reboot_instance(self, context, instance, block_device_info, reboot_type):

        @utils.synchronized(instance.uuid)
        def do_reboot_instance(context, instance, block_device_info, reboot_type):
            self._reboot_instance(context, instance, block_device_info, reboot_type)
        do_reboot_instance(context, instance, block_device_info, reboot_type)

    def _reboot_instance(self, context, instance, block_device_info, reboot_type):
        """Reboot an instance on this host."""
        if reboot_type == 'SOFT':
            instance.task_state = task_states.REBOOT_PENDING
            expected_states = task_states.soft_reboot_states
        else:
            instance.task_state = task_states.REBOOT_PENDING_HARD
            expected_states = task_states.hard_reboot_states
        context = context.elevated()
        LOG.info('Rebooting instance', instance=instance)
        bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(context, instance.uuid)
        block_device_info = self._get_instance_block_device_info(context, instance, bdms=bdms)
        network_info = self.network_api.get_instance_nw_info(context, instance)
        accel_info = self._get_accel_info(context, instance)
        self._notify_about_instance_usage(context, instance, 'reboot.start')
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.REBOOT, phase=fields.NotificationPhase.START, bdms=bdms)
        instance.power_state = self._get_power_state(instance)
        instance.save(expected_task_state=expected_states)
        if instance.power_state != power_state.RUNNING:
            state = instance.power_state
            running = power_state.RUNNING
            LOG.warning('trying to reboot a non-running instance: (state: %(state)s expected: %(running)s)', {'state': state, 'running': running}, instance=instance)

        def bad_volumes_callback(bad_devices):
            self._handle_bad_volumes_detached(context, instance, bad_devices, block_device_info)
        try:
            if instance.vm_state == vm_states.RESCUED:
                new_vm_state = vm_states.RESCUED
            else:
                new_vm_state = vm_states.ACTIVE
            new_power_state = None
            if reboot_type == 'SOFT':
                instance.task_state = task_states.REBOOT_STARTED
                expected_state = task_states.REBOOT_PENDING
            else:
                instance.task_state = task_states.REBOOT_STARTED_HARD
                expected_state = task_states.REBOOT_PENDING_HARD
            instance.save(expected_task_state=expected_state)
            self.driver.reboot(context, instance, network_info, reboot_type, block_device_info=block_device_info, accel_info=accel_info, bad_volumes_callback=bad_volumes_callback)
        except Exception as error:
            with excutils.save_and_reraise_exception() as ctxt:
                exc_info = sys.exc_info()
                new_power_state = self._get_power_state(instance)
                if new_power_state == power_state.RUNNING:
                    LOG.warning('Reboot failed but instance is running', instance=instance)
                    compute_utils.add_instance_fault_from_exc(context, instance, error, exc_info)
                    self._notify_about_instance_usage(context, instance, 'reboot.error', fault=error)
                    compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.REBOOT, phase=fields.NotificationPhase.ERROR, exception=error, bdms=bdms)
                    ctxt.reraise = False
                else:
                    LOG.error('Cannot reboot instance: %s', error, instance=instance)
                    self._set_instance_obj_error_state(instance)
        if not new_power_state:
            new_power_state = self._get_power_state(instance)
        try:
            instance.power_state = new_power_state
            instance.vm_state = new_vm_state
            instance.task_state = None
            instance.save()
        except exception.InstanceNotFound:
            LOG.warning('Instance disappeared during reboot', instance=instance)
        self._notify_about_instance_usage(context, instance, 'reboot.end')
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.REBOOT, phase=fields.NotificationPhase.END, bdms=bdms)

    @delete_image_on_error
    def _do_snapshot_instance(self, context, image_id, instance):
        self._snapshot_instance(context, image_id, instance, task_states.IMAGE_BACKUP)

    @wrap_exception()
    @reverts_task_state
    @wrap_instance_event(prefix='compute')
    @wrap_instance_fault
    def backup_instance(self, context, image_id, instance, backup_type, rotation):
        """Backup an instance on this host.

        :param backup_type: daily | weekly
        :param rotation: int representing how many backups to keep around
        """
        self._do_snapshot_instance(context, image_id, instance)
        self._rotate_backups(context, instance, backup_type, rotation)

    @wrap_exception()
    @reverts_task_state
    @wrap_instance_event(prefix='compute')
    @wrap_instance_fault
    @delete_image_on_error
    def snapshot_instance(self, context, image_id, instance):
        """Snapshot an instance on this host.

        :param context: security context
        :param image_id: glance.db.sqlalchemy.models.Image.Id
        :param instance: a nova.objects.instance.Instance object
        """
        try:
            instance.task_state = task_states.IMAGE_SNAPSHOT
            instance.save(expected_task_state=task_states.IMAGE_SNAPSHOT_PENDING)
        except exception.InstanceNotFound:
            LOG.debug('Instance not found, could not set state %s for instance.', task_states.IMAGE_SNAPSHOT, instance=instance)
            return
        except exception.UnexpectedDeletingTaskStateError:
            LOG.debug('Instance being deleted, snapshot cannot continue', instance=instance)
            return
        with self._snapshot_semaphore:
            self._snapshot_instance(context, image_id, instance, task_states.IMAGE_SNAPSHOT)

    def _snapshot_instance(self, context, image_id, instance, expected_task_state):
        context = context.elevated()
        instance.power_state = self._get_power_state(instance)
        try:
            instance.save()
            LOG.info('instance snapshotting', instance=instance)
            if instance.power_state != power_state.RUNNING:
                state = instance.power_state
                running = power_state.RUNNING
                LOG.warning('trying to snapshot a non-running instance: (state: %(state)s expected: %(running)s)', {'state': state, 'running': running}, instance=instance)
            self._notify_about_instance_usage(context, instance, 'snapshot.start')
            compute_utils.notify_about_instance_snapshot(context, instance, self.host, phase=fields.NotificationPhase.START, snapshot_image_id=image_id)

            def update_task_state(task_state, expected_state=expected_task_state):
                instance.task_state = task_state
                instance.save(expected_task_state=expected_state)
            with timeutils.StopWatch() as timer:
                self.driver.snapshot(context, instance, image_id, update_task_state)
            LOG.info('Took %0.2f seconds to snapshot the instance on the hypervisor.', timer.elapsed(), instance=instance)
            instance.task_state = None
            instance.save(expected_task_state=task_states.IMAGE_UPLOADING)
            self._notify_about_instance_usage(context, instance, 'snapshot.end')
            compute_utils.notify_about_instance_snapshot(context, instance, self.host, phase=fields.NotificationPhase.END, snapshot_image_id=image_id)
        except (exception.InstanceNotFound, exception.InstanceNotRunning, exception.UnexpectedDeletingTaskStateError):
            msg = 'Instance disappeared during snapshot'
            LOG.debug(msg, instance=instance)
            try:
                image = self.image_api.get(context, image_id)
                if image['status'] != 'active':
                    self.image_api.delete(context, image_id)
            except exception.ImageNotFound:
                LOG.debug('Image not found during clean up %s', image_id)
            except Exception:
                LOG.warning('Error while trying to clean up image %s', image_id, instance=instance)
        except exception.ImageNotFound:
            instance.task_state = None
            instance.save()
            LOG.warning('Image not found during snapshot', instance=instance)

    @messaging.expected_exceptions(NotImplementedError)
    @wrap_exception()
    def volume_snapshot_create(self, context, instance, volume_id, create_info):
        try:
            self.driver.volume_snapshot_create(context, instance, volume_id, create_info)
        except exception.InstanceNotRunning:
            LOG.debug('Instance disappeared during volume snapshot create', instance=instance)

    @messaging.expected_exceptions(NotImplementedError)
    @wrap_exception()
    def volume_snapshot_delete(self, context, instance, volume_id, snapshot_id, delete_info):
        try:
            self.driver.volume_snapshot_delete(context, instance, volume_id, snapshot_id, delete_info)
        except exception.InstanceNotRunning:
            LOG.debug('Instance disappeared during volume snapshot delete', instance=instance)

    @wrap_instance_fault
    def _rotate_backups(self, context, instance, backup_type, rotation):
        """Delete excess backups associated to an instance.

        Instances are allowed a fixed number of backups (the rotation number);
        this method deletes the oldest backups that exceed the rotation
        threshold.

        :param context: security context
        :param instance: Instance dict
        :param backup_type: a user-defined type, like "daily" or "weekly" etc.
        :param rotation: int representing how many backups to keep around;
            None if rotation shouldn't be used (as in the case of snapshots)
        """
        filters = {'property-image_type': 'backup', 'property-backup_type': backup_type, 'property-instance_uuid': instance.uuid}
        images = self.image_api.get_all(context, filters=filters, sort_key='created_at', sort_dir='desc')
        num_images = len(images)
        LOG.debug('Found %(num_images)d images (rotation: %(rotation)d)', {'num_images': num_images, 'rotation': rotation}, instance=instance)
        if num_images > rotation:
            excess = len(images) - rotation
            LOG.debug('Rotating out %d backups', excess, instance=instance)
            for i in range(excess):
                image = images.pop()
                image_id = image['id']
                LOG.debug('Deleting image %s', image_id, instance=instance)
                try:
                    self.image_api.delete(context, image_id)
                except exception.ImageNotFound:
                    LOG.info('Failed to find image %(image_id)s to delete', {'image_id': image_id}, instance=instance)
                except (exception.ImageDeleteConflict, Exception) as exc:
                    LOG.info('Failed to delete image %(image_id)s during deleting excess backups. Continuing for next image.. %(exc)s', {'image_id': image_id, 'exc': exc}, instance=instance)

    @wrap_exception()
    @reverts_task_state
    @wrap_instance_event(prefix='compute')
    @wrap_instance_fault
    def set_admin_password(self, context, instance, new_pass):
        """Set the root/admin password for an instance on this host.

        This is generally only called by API password resets after an
        image has been built.

        @param context: Nova auth context.
        @param instance: Nova instance object.
        @param new_pass: The admin password for the instance.
        """
        context = context.elevated()
        current_power_state = self._get_power_state(instance)
        expected_state = power_state.RUNNING
        if current_power_state != expected_state:
            instance.task_state = None
            instance.save(expected_task_state=task_states.UPDATING_PASSWORD)
            _msg = _('instance %s is not running') % instance.uuid
            raise exception.InstancePasswordSetFailed(instance=instance.uuid, reason=_msg)
        try:
            self.driver.set_admin_password(instance, new_pass)
            LOG.info('Admin password set', instance=instance)
            instance.task_state = None
            instance.save(expected_task_state=task_states.UPDATING_PASSWORD)
        except exception.InstanceAgentNotEnabled:
            with excutils.save_and_reraise_exception():
                LOG.debug('Guest agent is not enabled for the instance.', instance=instance)
                instance.task_state = None
                instance.save(expected_task_state=task_states.UPDATING_PASSWORD)
        except exception.SetAdminPasswdNotSupported:
            with excutils.save_and_reraise_exception():
                LOG.info('set_admin_password is not supported by this driver or guest instance.', instance=instance)
                instance.task_state = None
                instance.save(expected_task_state=task_states.UPDATING_PASSWORD)
        except NotImplementedError:
            LOG.warning('set_admin_password is not implemented by this driver or guest instance.', instance=instance)
            instance.task_state = None
            instance.save(expected_task_state=task_states.UPDATING_PASSWORD)
            raise NotImplementedError(_('set_admin_password is not implemented by this driver or guest instance.'))
        except exception.UnexpectedTaskStateError:
            raise
        except Exception:
            LOG.exception('set_admin_password failed', instance=instance)
            _msg = _('error setting admin password')
            raise exception.InstancePasswordSetFailed(instance=instance.uuid, reason=_msg)

    def _get_rescue_image(self, context, instance, rescue_image_ref=None):
        """Determine what image should be used to boot the rescue VM."""
        if not rescue_image_ref:
            system_meta = utils.instance_sys_meta(instance)
            rescue_image_ref = system_meta.get('image_base_image_ref')
        if not rescue_image_ref:
            LOG.warning("Unable to find a different image to use for rescue VM, using instance's current image", instance=instance)
            rescue_image_ref = instance.image_ref
        return objects.ImageMeta.from_image_ref(context, self.image_api, rescue_image_ref)

    @wrap_exception()
    @reverts_task_state
    @wrap_instance_event(prefix='compute')
    @wrap_instance_fault
    def rescue_instance(self, context, instance, rescue_password, rescue_image_ref, clean_shutdown):
        context = context.elevated()
        LOG.info('Rescuing', instance=instance)
        admin_password = rescue_password if rescue_password else utils.generate_password()
        network_info = self.network_api.get_instance_nw_info(context, instance)
        rescue_image_meta = self._get_rescue_image(context, instance, rescue_image_ref)
        bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(context, instance.uuid)
        block_device_info = self._get_instance_block_device_info(context, instance, bdms=bdms)
        extra_usage_info = {'rescue_image_name': self._get_image_name(rescue_image_meta)}
        self._notify_about_instance_usage(context, instance, 'rescue.start', extra_usage_info=extra_usage_info, network_info=network_info)
        compute_utils.notify_about_instance_rescue_action(context, instance, self.host, rescue_image_ref, phase=fields.NotificationPhase.START)
        try:
            self._power_off_instance(instance, clean_shutdown)
            self.driver.rescue(context, instance, network_info, rescue_image_meta, admin_password, block_device_info)
        except Exception as e:
            LOG.exception('Error trying to Rescue Instance', instance=instance)
            self._set_instance_obj_error_state(instance)
            raise exception.InstanceNotRescuable(instance_id=instance.uuid, reason=_('Driver Error: %s') % e)
        compute_utils.notify_usage_exists(self.notifier, context, instance, self.host, current_period=True)
        instance.vm_state = vm_states.RESCUED
        instance.task_state = None
        instance.power_state = self._get_power_state(instance)
        instance.launched_at = timeutils.utcnow()
        instance.save(expected_task_state=task_states.RESCUING)
        self._notify_about_instance_usage(context, instance, 'rescue.end', extra_usage_info=extra_usage_info, network_info=network_info)
        compute_utils.notify_about_instance_rescue_action(context, instance, self.host, rescue_image_ref, phase=fields.NotificationPhase.END)

    @wrap_exception()
    @reverts_task_state
    @wrap_instance_event(prefix='compute')
    @wrap_instance_fault
    def unrescue_instance(self, context, instance):
        orig_context = context
        context = context.elevated()
        LOG.info('Unrescuing', instance=instance)
        network_info = self.network_api.get_instance_nw_info(context, instance)
        self._notify_about_instance_usage(context, instance, 'unrescue.start', network_info=network_info)
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.UNRESCUE, phase=fields.NotificationPhase.START)
        with self._error_out_instance_on_exception(context, instance):
            self.driver.unrescue(orig_context, instance)
        instance.vm_state = vm_states.ACTIVE
        instance.task_state = None
        instance.power_state = self._get_power_state(instance)
        instance.save(expected_task_state=task_states.UNRESCUING)
        self._notify_about_instance_usage(context, instance, 'unrescue.end', network_info=network_info)
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.UNRESCUE, phase=fields.NotificationPhase.END)

    @wrap_exception()
    @wrap_instance_event(prefix='compute')
    @errors_out_migration
    @wrap_instance_fault
    def confirm_resize(self, context, instance, migration):
        """Confirms a migration/resize and deletes the 'old' instance.

        This is called from the API and runs on the source host.

        Nothing needs to happen on the destination host at this point since
        the instance is already running there. This routine just cleans up the
        source host.
        """

        @utils.synchronized(instance.uuid)
        def do_confirm_resize(context, instance, migration):
            LOG.debug('Going to confirm migration %s', migration.id, instance=instance)
            if migration.status == 'confirmed':
                LOG.info('Migration %s is already confirmed', migration.id, instance=instance)
                return
            if migration.status not in ('finished', 'confirming'):
                LOG.warning("Unexpected confirmation status '%(status)s' of migration %(id)s, exit confirmation process", {'status': migration.status, 'id': migration.id}, instance=instance)
                return
            expected_attrs = ['metadata', 'system_metadata', 'flavor']
            try:
                instance = objects.Instance.get_by_uuid(context, instance.uuid, expected_attrs=expected_attrs)
            except exception.InstanceNotFound:
                LOG.info('Instance is not found during confirmation', instance=instance)
                return
            with self._error_out_instance_on_exception(context, instance):
                try:
                    self._confirm_resize(context, instance, migration=migration)
                except Exception:
                    with excutils.save_and_reraise_exception(logger=LOG):
                        LOG.exception('Confirm resize failed on source host %s. Resource allocations in the placement service will be removed regardless because the instance is now on the destination host %s. You can try hard rebooting the instance to correct its state.', self.host, migration.dest_compute, instance=instance)
                finally:
                    self._delete_allocation_after_move(context, instance, migration)
                    self._delete_scheduler_instance_info(context, instance.uuid)
                    self._delete_stashed_flavor_info(instance)
        do_confirm_resize(context, instance, migration)

    def _get_updated_nw_info_with_pci_mapping(self, nw_info, pci_mapping):
        updated_nw_info = nw_info
        if nw_info and pci_mapping:
            updated_nw_info = copy.deepcopy(nw_info)
            for vif in updated_nw_info:
                if vif['vnic_type'] in network_model.VNIC_TYPES_SRIOV:
                    try:
                        vif_pci_addr = vif['profile']['pci_slot']
                        new_addr = pci_mapping[vif_pci_addr].address
                        vif['profile']['pci_slot'] = new_addr
                        LOG.debug("Updating VIF's PCI address for VIF %(id)s. Original value %(orig_val)s, new value %(new_val)s", {'id': vif['id'], 'orig_val': vif_pci_addr, 'new_val': new_addr})
                    except (KeyError, AttributeError):
                        with excutils.save_and_reraise_exception():
                            LOG.error('Unexpected error when updating network information with PCI mapping.')
        return updated_nw_info

    def _confirm_resize(self, context, instance, migration=None):
        """Destroys the source instance."""
        self._notify_about_instance_usage(context, instance, 'resize.confirm.start')
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.RESIZE_CONFIRM, phase=fields.NotificationPhase.START)
        self.network_api.setup_networks_on_host(context, instance, migration.source_compute, teardown=True)
        network_info = self.network_api.get_instance_nw_info(context, instance)
        pci_mapping = instance.migration_context.get_pci_mapping_for_migration(True)
        network_info = self._get_updated_nw_info_with_pci_mapping(network_info, pci_mapping)
        self.driver.confirm_migration(context, migration, instance, network_info)
        self.rt.drop_move_claim_at_source(context, instance, migration)
        p_state = instance.power_state
        vm_state = None
        if p_state == power_state.SHUTDOWN:
            vm_state = vm_states.STOPPED
            LOG.debug("Resized/migrated instance is powered off. Setting vm_state to '%s'.", vm_state, instance=instance)
        else:
            vm_state = vm_states.ACTIVE
        instance.vm_state = vm_state
        instance.task_state = None
        instance.save(expected_task_state=[None, task_states.DELETING, task_states.SOFT_DELETING])
        self._notify_about_instance_usage(context, instance, 'resize.confirm.end', network_info=network_info)
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.RESIZE_CONFIRM, phase=fields.NotificationPhase.END)

    def _delete_allocation_after_move(self, context, instance, migration):
        """Deletes resource allocations held by the migration record against
        the source compute node resource provider after a confirmed cold /
        successful live migration.
        """
        try:
            self.reportclient.delete_allocation_for_instance(context, migration.uuid, consumer_type='migration')
        except exception.AllocationDeleteFailed:
            LOG.error('Deleting allocation in placement for migration %(migration_uuid)s failed. The instance %(instance_uuid)s will be put to ERROR state but the allocation held by the migration is leaked.', {'instance_uuid': instance.uuid, 'migration_uuid': migration.uuid})
            raise

    def _delete_stashed_flavor_info(self, instance):
        """Remove information about the flavor change after a resize."""
        instance.old_flavor = None
        instance.new_flavor = None
        instance.system_metadata.pop('old_vm_state', None)
        instance.save()

    @wrap_exception()
    @wrap_instance_event(prefix='compute')
    @errors_out_migration
    @wrap_instance_fault
    def confirm_snapshot_based_resize_at_source(self, ctxt, instance, migration):
        """Confirms a snapshot-based resize on the source host.

        Cleans the guest from the source hypervisor including disks and drops
        the MoveClaim which will free up "old_flavor" usage from the
        ResourceTracker.

        Deletes the allocations held by the migration consumer against the
        source compute node resource provider.

        :param ctxt: nova auth request context targeted at the source cell
        :param instance: Instance object being resized which should have the
            "old_flavor" attribute set
        :param migration: Migration object for the resize operation
        """

        @utils.synchronized(instance.uuid)
        def do_confirm():
            LOG.info('Confirming resize on source host.', instance=instance)
            with self._error_out_instance_on_exception(ctxt, instance):
                try:
                    self._confirm_snapshot_based_resize_at_source(ctxt, instance, migration)
                except Exception:
                    with excutils.save_and_reraise_exception(logger=LOG):
                        LOG.exception('Confirm resize failed on source host %s. Resource allocations in the placement service will be removed regardless because the instance is now on the destination host %s. You can try hard rebooting the instance to correct its state.', self.host, migration.dest_compute, instance=instance)
                finally:
                    self._delete_allocation_after_move(ctxt, instance, migration)
        do_confirm()

    def _confirm_snapshot_based_resize_at_source(self, ctxt, instance, migration):
        """Private version of confirm_snapshot_based_resize_at_source

        This allows the main method to be decorated with error handlers.

        :param ctxt: nova auth request context targeted at the source cell
        :param instance: Instance object being resized which should have the
            "old_flavor" attribute set
        :param migration: Migration object for the resize operation
        """
        network_info = self.network_api.get_instance_nw_info(ctxt, instance)
        LOG.debug('Cleaning up guest from source hypervisor including disks.', instance=instance)
        self.driver.cleanup(ctxt, instance, network_info, block_device_info=None, destroy_disks=True, destroy_vifs=False)
        self._confirm_snapshot_based_resize_delete_port_bindings(ctxt, instance)
        self._delete_volume_attachments(ctxt, instance.get_bdms())
        self.rt.drop_move_claim_at_source(ctxt, instance, migration)

    def _confirm_snapshot_based_resize_delete_port_bindings(self, ctxt, instance):
        """Delete port bindings for the source host when confirming
        snapshot-based resize on the source host."

        :param ctxt: nova auth RequestContext
        :param instance: Instance object that was resized/cold migrated
        """
        LOG.debug('Deleting port bindings for source host.', instance=instance)
        try:
            self.network_api.cleanup_instance_network_on_host(ctxt, instance, self.host)
        except exception.PortBindingDeletionFailed as e:
            LOG.error('Failed to delete port bindings from source host. Error: %s', str(e), instance=instance)

    def _delete_volume_attachments(self, ctxt, bdms):
        """Deletes volume attachment records for the given bdms.

        This method will log but not re-raise any exceptions if the volume
        attachment delete fails.

        :param ctxt: nova auth request context used to make
            DELETE /attachments/{attachment_id} requests to cinder.
        :param bdms: objects.BlockDeviceMappingList representing volume
            attachments to delete based on BlockDeviceMapping.attachment_id.
        """
        for bdm in bdms:
            if bdm.attachment_id:
                try:
                    self.volume_api.attachment_delete(ctxt, bdm.attachment_id)
                except Exception as e:
                    LOG.error('Failed to delete volume attachment with ID %s. Error: %s', bdm.attachment_id, str(e), instance_uuid=bdm.instance_uuid)

    @wrap_exception()
    @reverts_task_state
    @wrap_instance_event(prefix='compute')
    @errors_out_migration
    @wrap_instance_fault
    def revert_snapshot_based_resize_at_dest(self, ctxt, instance, migration):
        """Reverts a snapshot-based resize at the destination host.

        Cleans the guest from the destination compute service host hypervisor
        and related resources (ports, volumes) and frees resource usage from
        the compute service on that host.

        :param ctxt: nova auth request context targeted at the target cell
        :param instance: Instance object whose vm_state is "resized" and
            task_state is "resize_reverting".
        :param migration: Migration object whose status is "reverting".
        """
        compute_utils.notify_usage_exists(self.notifier, ctxt, instance, self.host, current_period=True)

        @utils.synchronized(instance.uuid)
        def do_revert():
            LOG.info('Reverting resize on destination host.', instance=instance)
            with self._error_out_instance_on_exception(ctxt, instance):
                self._revert_snapshot_based_resize_at_dest(ctxt, instance, migration)
        do_revert()
        try:
            self._delete_scheduler_instance_info(ctxt, instance.uuid)
            self.instance_events.clear_events_for_instance(instance)
        except Exception as e:
            LOG.warning('revert_snapshot_based_resize_at_dest failed during post-processing. Error: %s', e, instance=instance)

    def _revert_snapshot_based_resize_at_dest(self, ctxt, instance, migration):
        """Private version of revert_snapshot_based_resize_at_dest.

        This allows the main method to be decorated with error handlers.

        :param ctxt: nova auth request context targeted at the target cell
        :param instance: Instance object whose vm_state is "resized" and
            task_state is "resize_reverting".
        :param migration: Migration object whose status is "reverting".
        """
        network_info = self.network_api.get_instance_nw_info(ctxt, instance)
        bdms = instance.get_bdms()
        block_device_info = self._get_instance_block_device_info(ctxt, instance, bdms=bdms)
        LOG.debug('Destroying guest from destination hypervisor including disks.', instance=instance)
        self.driver.destroy(ctxt, instance, network_info, block_device_info=block_device_info)
        with utils.temporary_mutation(migration, dest_compute=migration.source_compute):
            LOG.debug('Activating port bindings for source host %s.', migration.source_compute, instance=instance)
            self.network_api.migrate_instance_start(ctxt, instance, migration)
        LOG.debug('Deleting port bindings for target host %s.', self.host, instance=instance)
        try:
            self.network_api.cleanup_instance_network_on_host(ctxt, instance, self.host)
        except exception.PortBindingDeletionFailed as e:
            LOG.error('Failed to delete port bindings from target host. Error: %s', str(e), instance=instance)
        LOG.debug('Deleting volume attachments for target host.', instance=instance)
        self._delete_volume_attachments(ctxt, bdms)
        self.rt.drop_move_claim_at_dest(ctxt, instance, migration)

    def _revert_instance_flavor_host_node(self, instance, migration):
        """Revert host, node and flavor fields after a resize-revert."""
        self._set_instance_info(instance, instance.old_flavor)
        instance.host = migration.source_compute
        instance.node = migration.source_node
        instance.save(expected_task_state=[task_states.RESIZE_REVERTING])

    @wrap_exception()
    @reverts_task_state
    @wrap_instance_event(prefix='compute')
    @errors_out_migration
    @wrap_instance_fault
    def finish_revert_snapshot_based_resize_at_source(self, ctxt, instance, migration):
        """Reverts a snapshot-based resize at the source host.

        Spawn the guest and re-connect volumes/VIFs on the source host and
        revert the instance to use the old_flavor for resource usage reporting.

        Updates allocations in the placement service to move the source node
        allocations, held by the migration record, to the instance and drop
        the allocations held by the instance on the destination node.

        :param ctxt: nova auth request context targeted at the target cell
        :param instance: Instance object whose vm_state is "resized" and
            task_state is "resize_reverting".
        :param migration: Migration object whose status is "reverting".
        """

        @utils.synchronized(instance.uuid)
        def do_revert():
            LOG.info('Reverting resize on source host.', instance=instance)
            with self._error_out_instance_on_exception(ctxt, instance):
                self._finish_revert_snapshot_based_resize_at_source(ctxt, instance, migration)
        try:
            do_revert()
        finally:
            self._delete_stashed_flavor_info(instance)
        try:
            self._update_scheduler_instance_info(ctxt, instance)
        except Exception as e:
            LOG.warning('finish_revert_snapshot_based_resize_at_source failed during post-processing. Error: %s', e, instance=instance)

    def _finish_revert_snapshot_based_resize_at_source(self, ctxt, instance, migration):
        """Private version of finish_revert_snapshot_based_resize_at_source.

        This allows the main method to be decorated with error handlers.

        :param ctxt: nova auth request context targeted at the source cell
        :param instance: Instance object whose vm_state is "resized" and
            task_state is "resize_reverting".
        :param migration: Migration object whose status is "reverting".
        """
        old_vm_state = instance.system_metadata.get('old_vm_state', vm_states.ACTIVE)
        self._revert_instance_flavor_host_node(instance, migration)
        try:
            self._revert_allocation(ctxt, instance, migration)
        except exception.AllocationMoveFailed:
            LOG.error('Reverting allocation in placement for migration %(migration_uuid)s failed. You may need to manually remove the allocations for the migration consumer against the source node resource provider %(source_provider)s and the allocations for the instance consumer against the destination node resource provider %(dest_provider)s and then run the "nova-manage placement heal_allocations" command.', {'instance_uuid': instance.uuid, 'migration_uuid': migration.uuid, 'source_provider': migration.source_node, 'dest_provider': migration.dest_node}, instance=instance)
        bdms = instance.get_bdms()
        LOG.debug('Updating volume attachments for target host %s.', self.host, instance=instance)
        self._update_volume_attachments(ctxt, instance, bdms)
        LOG.debug('Updating port bindings for source host %s.', self.host, instance=instance)
        self._finish_revert_resize_network_migrate_finish(ctxt, instance, migration, provider_mappings=None)
        network_info = self.network_api.get_instance_nw_info(ctxt, instance)
        block_device_info = self._get_instance_block_device_info(ctxt, instance, bdms=bdms)
        power_on = old_vm_state == vm_states.ACTIVE
        driver_error = None
        try:
            self.driver.finish_revert_migration(ctxt, instance, network_info, migration, block_device_info=block_device_info, power_on=power_on)
        except Exception as e:
            driver_error = e
            with excutils.save_and_reraise_exception(logger=LOG):
                LOG.error('An error occurred during finish_revert_migration. The instance may need to be hard rebooted. Error: %s', driver_error, instance=instance)
        else:
            instance.drop_migration_context()
            vm_state = vm_states.ACTIVE if power_on else vm_states.STOPPED
            self._update_instance_after_spawn(instance, vm_state=vm_state)
            instance.save(expected_task_state=[task_states.RESIZE_REVERTING])
        finally:
            LOG.debug('Completing volume attachments for instance on source host.', instance=instance)
            with excutils.save_and_reraise_exception(reraise=driver_error is not None, logger=LOG):
                self._complete_volume_attachments(ctxt, bdms)
        migration.status = 'reverted'
        migration.save()

    @wrap_exception()
    @reverts_task_state
    @wrap_instance_event(prefix='compute')
    @errors_out_migration
    @wrap_instance_fault
    def revert_resize(self, context, instance, migration, request_spec):
        """Destroys the new instance on the destination machine.

        Reverts the model changes, and powers on the old instance on the
        source machine.

        """
        compute_utils.notify_usage_exists(self.notifier, context, instance, self.host, current_period=True)
        with self._error_out_instance_on_exception(context, instance):
            self.network_api.setup_networks_on_host(context, instance, teardown=True)
            self.network_api.migrate_instance_start(context, instance, migration)
            network_info = self.network_api.get_instance_nw_info(context, instance)
            bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(context, instance.uuid)
            block_device_info = self._get_instance_block_device_info(context, instance, bdms=bdms)
            destroy_disks = not self._is_instance_storage_shared(context, instance, host=migration.source_compute)
            self.driver.destroy(context, instance, network_info, block_device_info, destroy_disks)
            self._terminate_volume_connections(context, instance, bdms)
            self.rt.drop_move_claim_at_dest(context, instance, migration)
            self.compute_rpcapi.finish_revert_resize(context, instance, migration, migration.source_compute, request_spec)

    def _finish_revert_resize_network_migrate_finish(self, context, instance, migration, provider_mappings):
        """Causes port binding to be updated. In some Neutron or port
        configurations - see NetworkModel.get_bind_time_events() - we
        expect the vif-plugged event from Neutron immediately and wait for it.
        The rest of the time, the event is expected further along in the
        virt driver, so we don't wait here.

        :param context: The request context.
        :param instance: The instance undergoing the revert resize.
        :param migration: The Migration object of the resize being reverted.
        :param provider_mappings: a dict of list of resource provider uuids
            keyed by port uuid
        :raises: eventlet.timeout.Timeout or
                 exception.VirtualInterfacePlugException.
        """
        network_info = instance.get_network_info()
        events = []
        deadline = CONF.vif_plugging_timeout
        if deadline and network_info:
            events = network_info.get_bind_time_events(migration)
            if events:
                LOG.debug('Will wait for bind-time events: %s', events)
        error_cb = self._neutron_failed_migration_callback
        try:
            with self.virtapi.wait_for_instance_event(instance, events, deadline=deadline, error_callback=error_cb):
                with utils.temporary_mutation(migration, dest_compute=migration.source_compute):
                    self.network_api.migrate_instance_finish(context, instance, migration, provider_mappings)
        except eventlet.timeout.Timeout:
            with excutils.save_and_reraise_exception():
                LOG.error('Timeout waiting for Neutron events: %s', events, instance=instance)

    @wrap_exception()
    @reverts_task_state
    @wrap_instance_event(prefix='compute')
    @errors_out_migration
    @wrap_instance_fault
    def finish_revert_resize(self, context, instance, migration, request_spec):
        """Finishes the second half of reverting a resize on the source host.

        Bring the original source instance state back (active/shutoff) and
        revert the resized attributes in the database.

        """
        try:
            self._finish_revert_resize(context, instance, migration, request_spec)
        finally:
            self._delete_stashed_flavor_info(instance)

    def _finish_revert_resize(self, context, instance, migration, request_spec=None):
        """Inner version of finish_revert_resize."""
        with self._error_out_instance_on_exception(context, instance):
            bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(context, instance.uuid)
            self._notify_about_instance_usage(context, instance, 'resize.revert.start')
            compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.RESIZE_REVERT, phase=fields.NotificationPhase.START, bdms=bdms)
            old_vm_state = instance.system_metadata.get('old_vm_state', vm_states.ACTIVE)
            self._revert_instance_flavor_host_node(instance, migration)
            try:
                source_allocations = self._revert_allocation(context, instance, migration)
            except exception.AllocationMoveFailed:
                LOG.error('Reverting allocation in placement for migration %(migration_uuid)s failed. The instance %(instance_uuid)s will be put into ERROR state but the allocation held by the migration is leaked.', {'instance_uuid': instance.uuid, 'migration_uuid': migration.uuid})
                raise
            provider_mappings = self._fill_provider_mapping_based_on_allocs(context, source_allocations, request_spec)
            self.network_api.setup_networks_on_host(context, instance, migration.source_compute)
            self._finish_revert_resize_network_migrate_finish(context, instance, migration, provider_mappings)
            network_info = self.network_api.get_instance_nw_info(context, instance)
            self._update_volume_attachments(context, instance, bdms)
            block_device_info = self._get_instance_block_device_info(context, instance, refresh_conn_info=True, bdms=bdms)
            power_on = old_vm_state != vm_states.STOPPED
            self.driver.finish_revert_migration(context, instance, network_info, migration, block_device_info, power_on)
            instance.drop_migration_context()
            instance.launched_at = timeutils.utcnow()
            instance.save(expected_task_state=task_states.RESIZE_REVERTING)
            self._complete_volume_attachments(context, bdms)
            LOG.info("Updating instance to original state: '%s'", old_vm_state, instance=instance)
            if power_on:
                instance.vm_state = vm_states.ACTIVE
                instance.task_state = None
                instance.save()
            else:
                instance.task_state = task_states.POWERING_OFF
                instance.save()
                self.stop_instance(context, instance=instance, clean_shutdown=True)
            self._notify_about_instance_usage(context, instance, 'resize.revert.end')
            compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.RESIZE_REVERT, phase=fields.NotificationPhase.END, bdms=bdms)

    def _fill_provider_mapping_based_on_allocs(self, context, allocations, request_spec):
        """Fills and returns the request group - resource provider mapping
        based on the allocation passed in.

        :param context: The security context
        :param allocation: allocation dict keyed by RP UUID.
        :param request_spec: The RequestSpec object associated with the
            operation
        :returns: None if the request_spec is None. Otherwise a mapping
            between RequestGroup requester_id, currently Neutron port_id,
            and a list of resource provider UUIDs providing resource for
            that RequestGroup.
        """
        if request_spec:
            scheduler_utils.fill_provider_mapping_based_on_allocation(context, self.reportclient, request_spec, allocations)
            provider_mappings = self._get_request_group_mapping(request_spec)
        else:
            provider_mappings = None
        return provider_mappings

    def _revert_allocation(self, context, instance, migration):
        """Revert an allocation that is held by migration to our instance."""
        orig_alloc = self.reportclient.get_allocations_for_consumer(context, migration.uuid)
        if not orig_alloc:
            LOG.error('Did not find resource allocations for migration %s on source node %s. Unable to revert source node allocations back to the instance.', migration.uuid, migration.source_node, instance=instance)
            return False
        LOG.info('Swapping old allocation on %(rp_uuids)s held by migration %(mig)s for instance', {'rp_uuids': orig_alloc.keys(), 'mig': migration.uuid}, instance=instance)
        self.reportclient.move_allocations(context, migration.uuid, instance.uuid)
        return orig_alloc

    def _prep_resize(self, context, image, instance, instance_type, filter_properties, node, migration, request_spec, clean_shutdown=True):
        if not filter_properties:
            filter_properties = {}
        if not instance.host:
            self._set_instance_obj_error_state(instance)
            msg = _('Instance has no source host')
            raise exception.MigrationError(reason=msg)
        same_host = instance.host == self.host
        if same_host and instance_type.id == instance['instance_type_id']:
            if not self.driver.capabilities.get('supports_migrate_to_same_host', False):
                raise exception.InstanceFaultRollback(inner_exception=exception.UnableToMigrateToSelf(instance_id=instance.uuid, host=self.host))
        instance.new_flavor = instance_type
        vm_state = instance.vm_state
        LOG.debug('Stashing vm_state: %s', vm_state, instance=instance)
        instance.system_metadata['old_vm_state'] = vm_state
        instance.save()
        if not isinstance(request_spec, objects.RequestSpec):
            request_spec = objects.RequestSpec.from_primitives(context, request_spec, filter_properties)
        provider_mapping = self._get_request_group_mapping(request_spec)
        if provider_mapping:
            try:
                compute_utils.update_pci_request_spec_with_allocated_interface_name(context, self.reportclient, instance.pci_requests.requests, provider_mapping)
            except (exception.AmbiguousResourceProviderForPCIRequest, exception.UnexpectedResourceProviderNameForPCIRequest) as e:
                raise exception.BuildAbortException(reason=str(e), instance_uuid=instance.uuid)
        limits = filter_properties.get('limits', {})
        allocs = self.reportclient.get_allocations_for_consumer(context, instance.uuid)
        with self.rt.resize_claim(context, instance, instance_type, node, migration, allocs, image_meta=image, limits=limits) as claim:
            LOG.info('Migrating', instance=instance)
            self.compute_rpcapi.resize_instance(context, instance, claim.migration, image, instance_type, request_spec, clean_shutdown)

    def _send_prep_resize_notifications(self, context, instance, phase, flavor):
        """Send "resize.prep.*" notifications.

        :param context: nova auth request context
        :param instance: The instance being resized
        :param phase: The phase of the action (NotificationPhase enum)
        :param flavor: The (new) flavor for the resize (same as existing
            instance.flavor for a cold migration)
        """
        if phase == fields.NotificationPhase.START:
            compute_utils.notify_usage_exists(self.notifier, context, instance, self.host, current_period=True)
        extra_usage_info = None
        if phase == fields.NotificationPhase.END:
            extra_usage_info = dict(new_instance_type=flavor.name, new_instance_type_id=flavor.id)
        self._notify_about_instance_usage(context, instance, 'resize.prep.%s' % phase, extra_usage_info=extra_usage_info)
        compute_utils.notify_about_resize_prep_instance(context, instance, self.host, phase, flavor)

    @wrap_exception()
    @reverts_task_state
    @wrap_instance_event(prefix='compute')
    @wrap_instance_fault
    def prep_resize(self, context, image, instance, flavor, request_spec, filter_properties, node, clean_shutdown, migration, host_list):
        """Initiates the process of moving a running instance to another host.

        Possibly changes the VCPU, RAM and disk size in the process.

        This is initiated from conductor and runs on the destination host.

        The main purpose of this method is performing some checks on the
        destination host and making a claim for resources. If the claim fails
        then a reschedule to another host may be attempted which involves
        calling back to conductor to start the process over again.
        """
        if node is None:
            node = self._get_nodename(instance, refresh=True)
        instance_state = instance.vm_state
        with self._error_out_instance_on_exception(context, instance, instance_state=instance_state), errors_out_migration_ctxt(migration):
            self._send_prep_resize_notifications(context, instance, fields.NotificationPhase.START, flavor)
            try:
                scheduler_hints = self._get_scheduler_hints(filter_properties, request_spec)
                try:
                    self._validate_instance_group_policy(context, instance, scheduler_hints)
                except exception.RescheduledException as e:
                    raise exception.InstanceFaultRollback(inner_exception=e)
                self._prep_resize(context, image, instance, flavor, filter_properties, node, migration, request_spec, clean_shutdown)
            except exception.BuildAbortException:
                with excutils.save_and_reraise_exception():
                    self._revert_allocation(context, instance, migration)
            except Exception:
                self._revert_allocation(context, instance, migration)
                exc_info = sys.exc_info()
                self._reschedule_resize_or_reraise(context, instance, exc_info, flavor, request_spec, filter_properties, host_list)
            finally:
                self._send_prep_resize_notifications(context, instance, fields.NotificationPhase.END, flavor)

    def _reschedule_resize_or_reraise(self, context, instance, exc_info, instance_type, request_spec, filter_properties, host_list):
        """Try to re-schedule the resize or re-raise the original error to
        error out the instance.
        """
        if not filter_properties:
            filter_properties = {}
        rescheduled = False
        instance_uuid = instance.uuid
        try:
            retry = filter_properties.get('retry')
            if retry:
                LOG.debug('Rescheduling, attempt %d', retry['num_attempts'], instance_uuid=instance_uuid)
                task_state = task_states.RESIZE_PREP
                self._instance_update(context, instance, task_state=task_state)
                if exc_info:
                    retry['exc'] = traceback.format_exception_only(exc_info[0], exc_info[1])
                scheduler_hint = {'filter_properties': filter_properties}
                self.compute_task_api.resize_instance(context, instance, scheduler_hint, instance_type, request_spec=request_spec, host_list=host_list)
                rescheduled = True
            else:
                LOG.debug('Retry info not present, will not reschedule', instance_uuid=instance_uuid)
                rescheduled = False
        except Exception as error:
            rescheduled = False
            LOG.exception('Error trying to reschedule', instance_uuid=instance_uuid)
            compute_utils.add_instance_fault_from_exc(context, instance, error, exc_info=sys.exc_info())
            self._notify_about_instance_usage(context, instance, 'resize.error', fault=error)
            compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.RESIZE, phase=fields.NotificationPhase.ERROR, exception=error)
        if rescheduled:
            self._log_original_error(exc_info, instance_uuid)
            compute_utils.add_instance_fault_from_exc(context, instance, exc_info[1], exc_info=exc_info)
            self._notify_about_instance_usage(context, instance, 'resize.error', fault=exc_info[1])
            compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.RESIZE, phase=fields.NotificationPhase.ERROR, exception=exc_info[1])
        else:
            exc = exc_info[1] or exc_info[0]()
            if exc.__traceback__ is not exc_info[2]:
                raise exc.with_traceback(exc_info[2])
            raise exc

    @messaging.expected_exceptions(exception.MigrationPreCheckError)
    @wrap_exception()
    @wrap_instance_event(prefix='compute')
    @wrap_instance_fault
    def prep_snapshot_based_resize_at_dest(self, ctxt, instance, flavor, nodename, migration, limits):
        """Performs pre-cross-cell resize resource claim on the dest host.

        This runs on the destination host in a cross-cell resize operation
        before the resize is actually started.

        Performs a resize_claim for resources that are not claimed in placement
        like PCI devices and NUMA topology.

        Note that this is different from same-cell prep_resize in that this:

        * Does not RPC cast to the source compute, that is orchestrated from
          conductor.
        * This does not reschedule on failure, conductor handles that since
          conductor is synchronously RPC calling this method. As such, the
          reverts_task_state decorator is not used on this method.

        :param ctxt: user auth request context
        :param instance: the instance being resized
        :param flavor: the flavor being resized to (unchanged for cold migrate)
        :param nodename: Name of the target compute node
        :param migration: nova.objects.Migration object for the operation
        :param limits: nova.objects.SchedulerLimits object of resource limits
        :returns: nova.objects.MigrationContext; the migration context created
            on the destination host during the resize_claim.
        :raises: nova.exception.MigrationPreCheckError if the pre-check
            validation fails for the given host selection
        """
        LOG.debug('Checking if we can cross-cell migrate instance to this host (%s).', self.host, instance=instance)
        self._send_prep_resize_notifications(ctxt, instance, fields.NotificationPhase.START, flavor)
        try:
            allocations = self.reportclient.get_allocs_for_consumer(ctxt, instance.uuid)['allocations']
            self.rt.resize_claim(ctxt, instance, flavor, nodename, migration, allocations, image_meta=instance.image_meta, limits=limits)
        except Exception as ex:
            err = str(ex)
            LOG.warning('Cross-cell resize pre-checks failed for this host (%s). Cleaning up. Failure: %s', self.host, err, instance=instance, exc_info=True)
            raise exception.MigrationPreCheckError(reason=_("Pre-checks failed on host '%(host)s'. Error: %(error)s") % {'host': self.host, 'error': err})
        finally:
            self._send_prep_resize_notifications(ctxt, instance, fields.NotificationPhase.END, flavor)
        return instance.migration_context

    @messaging.expected_exceptions(exception.InstancePowerOffFailure)
    @wrap_exception()
    @reverts_task_state
    @wrap_instance_event(prefix='compute')
    @errors_out_migration
    @wrap_instance_fault
    def prep_snapshot_based_resize_at_source(self, ctxt, instance, migration, snapshot_id=None):
        """Prepares the instance at the source host for cross-cell resize

        Performs actions like powering off the guest, upload snapshot data if
        the instance is not volume-backed, disconnecting volumes, unplugging
        VIFs and activating the destination host port bindings.

        :param ctxt: user auth request context targeted at source cell
        :param instance: nova.objects.Instance; the instance being resized.
            The expected instance.task_state is "resize_migrating" when calling
            this method, and the expected task_state upon successful completion
            is "resize_migrated".
        :param migration: nova.objects.Migration object for the operation.
            The expected migration.status is "pre-migrating" when calling this
            method and the expected status upon successful completion is
            "post-migrating".
        :param snapshot_id: ID of the image snapshot to upload if not a
            volume-backed instance
        :raises: nova.exception.InstancePowerOffFailure if stopping the
            instance fails
        """
        LOG.info('Preparing for snapshot based resize on source host %s.', self.host, instance=instance)
        self._prep_snapshot_based_resize_at_source(ctxt, instance, migration, snapshot_id=snapshot_id)

    @delete_image_on_error
    def _snapshot_for_resize(self, ctxt, image_id, instance):
        """Uploads snapshot for the instance during a snapshot-based resize

        If the snapshot operation fails the image will be deleted.

        :param ctxt: the nova auth request context for the resize operation
        :param image_id: the snapshot image ID
        :param instance: the instance to snapshot/resize
        """
        LOG.debug('Uploading snapshot data for image %s', image_id, instance=instance)
        update_task_state = lambda *args, **kwargs: None
        with timeutils.StopWatch() as timer:
            self.driver.snapshot(ctxt, instance, image_id, update_task_state)
            LOG.debug('Took %0.2f seconds to snapshot the instance on the hypervisor.', timer.elapsed(), instance=instance)

    def _prep_snapshot_based_resize_at_source(self, ctxt, instance, migration, snapshot_id=None):
        """Private method for prep_snapshot_based_resize_at_source so calling
        code can handle errors and perform rollbacks as necessary.
        """
        network_info = self.network_api.get_instance_nw_info(ctxt, instance)
        bdms = instance.get_bdms()
        self._send_resize_instance_notifications(ctxt, instance, bdms, network_info, fields.NotificationPhase.START)
        migration.status = 'migrating'
        migration.save()
        LOG.debug('Stopping instance', instance=instance)
        try:
            self._power_off_instance(instance)
        except Exception as e:
            LOG.exception('Failed to power off instance.', instance=instance)
            raise exception.InstancePowerOffFailure(reason=str(e))
        instance.power_state = self._get_power_state(instance)
        if snapshot_id:
            self._snapshot_for_resize(ctxt, snapshot_id, instance)
        block_device_info = self._get_instance_block_device_info(ctxt, instance, bdms=bdms)
        with self._error_out_instance_on_exception(ctxt, instance, instance_state=vm_states.ERROR):
            LOG.debug('Destroying guest on source host but retaining disks.', instance=instance)
            self.driver.destroy(ctxt, instance, network_info, block_device_info=block_device_info, destroy_disks=False)
            LOG.debug('Deleting volume attachments for the source host.', instance=instance)
            self._terminate_volume_connections(ctxt, instance, bdms)
            self.network_api.migrate_instance_start(ctxt, instance, migration)
            migration.status = 'post-migrating'
            migration.save()
            instance.task_state = task_states.RESIZE_MIGRATED
            instance.save(expected_task_state=task_states.RESIZE_MIGRATING)
        self._send_resize_instance_notifications(ctxt, instance, bdms, network_info, fields.NotificationPhase.END)
        self.instance_events.clear_events_for_instance(instance)

    @wrap_exception()
    @reverts_task_state
    @wrap_instance_event(prefix='compute')
    @wrap_instance_fault
    def resize_instance(self, context, instance, image, migration, flavor, clean_shutdown, request_spec):
        """Starts the migration of a running instance to another host.

        This is initiated from the destination host's ``prep_resize`` routine
        and runs on the source host.
        """
        try:
            self._resize_instance(context, instance, image, migration, flavor, clean_shutdown, request_spec)
        except Exception:
            with excutils.save_and_reraise_exception():
                self._revert_allocation(context, instance, migration)

    def _resize_instance(self, context, instance, image, migration, instance_type, clean_shutdown, request_spec):
        instance_state = instance.vm_state
        with self._error_out_instance_on_exception(context, instance, instance_state=instance_state), errors_out_migration_ctxt(migration):
            network_info = self.network_api.get_instance_nw_info(context, instance)
            migration.status = 'migrating'
            migration.save()
            instance.task_state = task_states.RESIZE_MIGRATING
            instance.save(expected_task_state=task_states.RESIZE_PREP)
            bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(context, instance.uuid)
            self._send_resize_instance_notifications(context, instance, bdms, network_info, fields.NotificationPhase.START)
            block_device_info = self._get_instance_block_device_info(context, instance, bdms=bdms)
            (timeout, retry_interval) = self._get_power_off_values(instance, clean_shutdown)
            disk_info = self.driver.migrate_disk_and_power_off(context, instance, migration.dest_host, instance_type, network_info, block_device_info, timeout, retry_interval)
            self._terminate_volume_connections(context, instance, bdms)
            self.network_api.migrate_instance_start(context, instance, migration)
            migration.status = 'post-migrating'
            migration.save()
            instance.host = migration.dest_compute
            instance.node = migration.dest_node
            instance.task_state = task_states.RESIZE_MIGRATED
            instance.save(expected_task_state=task_states.RESIZE_MIGRATING)
            self.compute_rpcapi.finish_resize(context, instance, migration, image, disk_info, migration.dest_compute, request_spec)
        self._send_resize_instance_notifications(context, instance, bdms, network_info, fields.NotificationPhase.END)
        self.instance_events.clear_events_for_instance(instance)

    def _send_resize_instance_notifications(self, context, instance, bdms, network_info, phase):
        """Send "resize.(start|end)" notifications.

        :param context: nova auth request context
        :param instance: The instance being resized
        :param bdms: BlockDeviceMappingList for the BDMs associated with the
            instance
        :param network_info: NetworkInfo for the instance info cache of ports
        :param phase: The phase of the action (NotificationPhase enum, either
            ``start`` or ``end``)
        """
        action = fields.NotificationAction.RESIZE
        self._notify_about_instance_usage(context, instance, '%s.%s' % (action, phase), network_info=network_info)
        compute_utils.notify_about_instance_action(context, instance, self.host, action=action, phase=phase, bdms=bdms)

    def _terminate_volume_connections(self, context, instance, bdms):
        connector = None
        for bdm in bdms:
            if bdm.is_volume:
                if bdm.attachment_id:
                    attachment_id = self.volume_api.attachment_create(context, bdm.volume_id, instance.uuid)['id']
                    self.volume_api.attachment_delete(context, bdm.attachment_id)
                    bdm.attachment_id = attachment_id
                    bdm.save()
                else:
                    if connector is None:
                        connector = self.driver.get_volume_connector(instance)
                    self.volume_api.terminate_connection(context, bdm.volume_id, connector)

    @staticmethod
    def _set_instance_info(instance, instance_type):
        instance.instance_type_id = instance_type.id
        instance.memory_mb = instance_type.memory_mb
        instance.vcpus = instance_type.vcpus
        instance.root_gb = instance_type.root_gb
        instance.ephemeral_gb = instance_type.ephemeral_gb
        instance.flavor = instance_type

    def _update_volume_attachments(self, context, instance, bdms):
        """Updates volume attachments using the virt driver host connector.

        :param context: nova.context.RequestContext - user request context
        :param instance: nova.objects.Instance
        :param bdms: nova.objects.BlockDeviceMappingList - the list of block
                     device mappings for the given instance
        """
        if bdms:
            connector = None
            for bdm in bdms:
                if bdm.is_volume and bdm.attachment_id:
                    if connector is None:
                        connector = self.driver.get_volume_connector(instance)
                    self.volume_api.attachment_update(context, bdm.attachment_id, connector, bdm.device_name)

    def _complete_volume_attachments(self, context, bdms):
        """Completes volume attachments for the instance

        :param context: nova.context.RequestContext - user request context
        :param bdms: nova.objects.BlockDeviceMappingList - the list of block
                     device mappings for the given instance
        """
        if bdms:
            for bdm in bdms:
                if bdm.is_volume and bdm.attachment_id:
                    self.volume_api.attachment_complete(context, bdm.attachment_id)

    def _finish_resize(self, context, instance, migration, disk_info, image_meta, bdms, request_spec):
        resize_instance = False
        old_instance_type_id = migration['old_instance_type_id']
        new_instance_type_id = migration['new_instance_type_id']
        old_flavor = instance.flavor
        old_vm_state = instance.system_metadata.get('old_vm_state', vm_states.ACTIVE)
        instance.old_flavor = old_flavor
        if old_instance_type_id != new_instance_type_id:
            new_flavor = instance.new_flavor
            self._set_instance_info(instance, new_flavor)
            for key in ('root_gb', 'swap', 'ephemeral_gb'):
                if old_flavor[key] != new_flavor[key]:
                    resize_instance = True
                    break
        instance.apply_migration_context()
        self.network_api.setup_networks_on_host(context, instance, migration.dest_compute)
        provider_mappings = self._get_request_group_mapping(request_spec)
        self.network_api.migrate_instance_finish(context, instance, migration, provider_mappings)
        network_info = self.network_api.get_instance_nw_info(context, instance)
        instance.task_state = task_states.RESIZE_FINISH
        instance.save(expected_task_state=task_states.RESIZE_MIGRATED)
        self._send_finish_resize_notifications(context, instance, bdms, network_info, fields.NotificationPhase.START)
        self._update_volume_attachments(context, instance, bdms)
        block_device_info = self._get_instance_block_device_info(context, instance, refresh_conn_info=True, bdms=bdms)
        power_on = old_vm_state != vm_states.STOPPED
        allocations = self.reportclient.get_allocs_for_consumer(context, instance.uuid)['allocations']
        try:
            self.driver.finish_migration(context, migration, instance, disk_info, network_info, image_meta, resize_instance, allocations, block_device_info, power_on)
        except Exception:
            with excutils.save_and_reraise_exception():
                if old_instance_type_id != new_instance_type_id:
                    self._set_instance_info(instance, old_flavor)
        self._complete_volume_attachments(context, bdms)
        migration.status = 'finished'
        migration.save()
        instance.vm_state = vm_states.RESIZED
        instance.task_state = None
        instance.launched_at = timeutils.utcnow()
        instance.save(expected_task_state=task_states.RESIZE_FINISH)
        return network_info

    @wrap_exception()
    @reverts_task_state
    @wrap_instance_event(prefix='compute')
    @errors_out_migration
    @wrap_instance_fault
    def finish_resize(self, context, disk_info, image, instance, migration, request_spec):
        """Completes the migration process.

        Sets up the newly transferred disk and turns on the instance at its
        new host machine.

        """
        try:
            self._finish_resize_helper(context, disk_info, image, instance, migration, request_spec)
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.info('Deleting allocations for old flavor on source node %s after finish_resize failure. You may be able to recover the instance by hard rebooting it.', migration.source_compute, instance=instance)
                self._delete_allocation_after_move(context, instance, migration)

    def _finish_resize_helper(self, context, disk_info, image, instance, migration, request_spec):
        """Completes the migration process.

        The caller must revert the instance's allocations if the migration
        process failed.
        """
        bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(context, instance.uuid)
        with self._error_out_instance_on_exception(context, instance):
            image_meta = objects.ImageMeta.from_dict(image)
            network_info = self._finish_resize(context, instance, migration, disk_info, image_meta, bdms, request_spec)
        self._update_scheduler_instance_info(context, instance)
        self._send_finish_resize_notifications(context, instance, bdms, network_info, fields.NotificationPhase.END)

    def _send_finish_resize_notifications(self, context, instance, bdms, network_info, phase):
        """Send notifications for the finish_resize flow.

        :param context: nova auth request context
        :param instance: The instance being resized
        :param bdms: BlockDeviceMappingList for the BDMs associated with the
            instance
        :param network_info: NetworkInfo for the instance info cache of ports
        :param phase: The phase of the action (NotificationPhase enum, either
            ``start`` or ``end``)
        """
        self._notify_about_instance_usage(context, instance, 'finish_resize.%s' % phase, network_info=network_info)
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.RESIZE_FINISH, phase=phase, bdms=bdms)

    @wrap_exception()
    @reverts_task_state
    @wrap_instance_event(prefix='compute')
    @errors_out_migration
    @wrap_instance_fault
    def finish_snapshot_based_resize_at_dest(self, ctxt, instance, migration, snapshot_id):
        """Finishes the snapshot-based resize at the destination compute.

        Sets up block devices and networking on the destination compute and
        spawns the guest.

        :param ctxt: nova auth request context targeted at the target cell DB
        :param instance: The Instance object being resized with the
            ``migration_context`` field set. Upon successful completion of this
            method the vm_state should be "resized", the task_state should be
            None, and migration context, host/node and flavor-related fields
            should be set on the instance.
        :param migration: The Migration object for this resize operation. Upon
            successful completion of this method the migration status should
            be "finished".
        :param snapshot_id: ID of the image snapshot created for a
            non-volume-backed instance, else None.
        """
        LOG.info('Finishing snapshot based resize on destination host %s.', self.host, instance=instance)
        with self._error_out_instance_on_exception(ctxt, instance):
            self._finish_snapshot_based_resize_at_dest(ctxt, instance, migration, snapshot_id)

    def _finish_snapshot_based_resize_at_dest(self, ctxt, instance, migration, snapshot_id):
        """Private variant of finish_snapshot_based_resize_at_dest so the
        caller can handle reverting resource allocations on failure and perform
        other generic error handling.
        """
        origin_image_ref = instance.image_ref
        if snapshot_id:
            instance.image_ref = snapshot_id
            image_meta = objects.ImageMeta.from_image_ref(ctxt, self.image_api, snapshot_id)
        else:
            image_meta = instance.image_meta
        resize = migration.migration_type == 'resize'
        instance.old_flavor = instance.flavor
        if resize:
            flavor = instance.new_flavor
            self._set_instance_info(instance, flavor)
        instance.apply_migration_context()
        instance.task_state = task_states.RESIZE_FINISH
        instance.save(expected_task_state=task_states.RESIZE_MIGRATED)
        bdms = instance.get_bdms()
        network_info = instance.get_network_info()
        self._send_finish_resize_notifications(ctxt, instance, bdms, network_info, fields.NotificationPhase.START)
        self._finish_snapshot_based_resize_at_dest_spawn(ctxt, instance, migration, image_meta, bdms)
        if snapshot_id:
            instance.image_ref = origin_image_ref
            compute_utils.delete_image(ctxt, instance, self.image_api, snapshot_id)
        migration.status = 'finished'
        migration.save()
        self._update_instance_after_spawn(instance, vm_state=vm_states.RESIZED)
        instance.host = migration.dest_compute
        instance.node = migration.dest_node
        instance.save(expected_task_state=task_states.RESIZE_FINISH)
        self._update_scheduler_instance_info(ctxt, instance)
        self._send_finish_resize_notifications(ctxt, instance, bdms, network_info, fields.NotificationPhase.END)

    def _finish_snapshot_based_resize_at_dest_spawn(self, ctxt, instance, migration, image_meta, bdms):
        """Sets up volumes and networking and spawns the guest on the dest host

        If the instance was stopped when the resize was initiated the guest
        will be created but remain in a shutdown power state.

        If the spawn fails, port bindings are rolled back to the source host
        and volume connections are terminated for this dest host.

        :param ctxt: nova auth request context
        :param instance: Instance object being migrated
        :param migration: Migration object for the operation
        :param image_meta: ImageMeta object used during driver.spawn
        :param bdms: BlockDeviceMappingList of BDMs for the instance
        """
        block_device_info = self._prep_block_device(ctxt, instance, bdms)
        allocations = self.reportclient.get_allocations_for_consumer(ctxt, instance.uuid)
        self.network_api.migrate_instance_finish(ctxt, instance, migration, provider_mappings=None)
        network_info = self.network_api.get_instance_nw_info(ctxt, instance)
        power_on = instance.system_metadata['old_vm_state'] == vm_states.ACTIVE
        try:
            self.driver.spawn(ctxt, instance, image_meta, injected_files=[], admin_password=None, allocations=allocations, network_info=network_info, block_device_info=block_device_info, power_on=power_on)
        except Exception:
            with excutils.save_and_reraise_exception(logger=LOG):
                try:
                    with utils.temporary_mutation(migration, dest_compute=migration.source_compute):
                        self.network_api.migrate_instance_start(ctxt, instance, migration)
                except Exception:
                    LOG.exception('Failed to activate port bindings on the source host: %s', migration.source_compute, instance=instance)
                for bdm in bdms:
                    if bdm.is_volume:
                        try:
                            self._remove_volume_connection(ctxt, bdm, instance, delete_attachment=True)
                        except Exception:
                            LOG.exception('Failed to remove volume connection on this host %s for volume %s.', self.host, bdm.volume_id, instance=instance)

    @wrap_exception()
    @wrap_instance_fault
    def add_fixed_ip_to_instance(self, context, network_id, instance):
        """Calls network_api to add new fixed_ip to instance
        then injects the new network info and resets instance networking.

        """
        self._notify_about_instance_usage(context, instance, 'create_ip.start')
        network_info = self.network_api.add_fixed_ip_to_instance(context, instance, network_id)
        self._inject_network_info(instance, network_info)
        instance.updated_at = timeutils.utcnow()
        instance.save()
        self._notify_about_instance_usage(context, instance, 'create_ip.end', network_info=network_info)

    @wrap_exception()
    @wrap_instance_fault
    def remove_fixed_ip_from_instance(self, context, address, instance):
        """Calls network_api to remove existing fixed_ip from instance
        by injecting the altered network info and resetting
        instance networking.
        """
        self._notify_about_instance_usage(context, instance, 'delete_ip.start')
        network_info = self.network_api.remove_fixed_ip_from_instance(context, instance, address)
        self._inject_network_info(instance, network_info)
        instance.updated_at = timeutils.utcnow()
        instance.save()
        self._notify_about_instance_usage(context, instance, 'delete_ip.end', network_info=network_info)

    @wrap_exception()
    @reverts_task_state
    @wrap_instance_event(prefix='compute')
    @wrap_instance_fault
    def pause_instance(self, context, instance):
        """Pause an instance on this host."""
        context = context.elevated()
        LOG.info('Pausing', instance=instance)
        self._notify_about_instance_usage(context, instance, 'pause.start')
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.PAUSE, phase=fields.NotificationPhase.START)
        self.driver.pause(instance)
        instance.power_state = self._get_power_state(instance)
        instance.vm_state = vm_states.PAUSED
        instance.task_state = None
        instance.save(expected_task_state=task_states.PAUSING)
        self._notify_about_instance_usage(context, instance, 'pause.end')
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.PAUSE, phase=fields.NotificationPhase.END)

    @wrap_exception()
    @reverts_task_state
    @wrap_instance_event(prefix='compute')
    @wrap_instance_fault
    def unpause_instance(self, context, instance):
        """Unpause a paused instance on this host."""
        context = context.elevated()
        LOG.info('Unpausing', instance=instance)
        self._notify_about_instance_usage(context, instance, 'unpause.start')
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.UNPAUSE, phase=fields.NotificationPhase.START)
        self.driver.unpause(instance)
        instance.power_state = self._get_power_state(instance)
        instance.vm_state = vm_states.ACTIVE
        instance.task_state = None
        instance.save(expected_task_state=task_states.UNPAUSING)
        self._notify_about_instance_usage(context, instance, 'unpause.end')
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.UNPAUSE, phase=fields.NotificationPhase.END)

    @wrap_exception()
    def host_power_action(self, context, action):
        """Reboots, shuts down or powers up the host."""
        return self.driver.host_power_action(action)

    @wrap_exception()
    def host_maintenance_mode(self, context, host, mode):
        """Start/Stop host maintenance window. On start, it triggers
        guest VMs evacuation.
        """
        return self.driver.host_maintenance_mode(host, mode)

    def _update_compute_provider_status(self, context, enabled):
        """Adds or removes the COMPUTE_STATUS_DISABLED trait for this host.

        For each ComputeNode managed by this service, adds or removes the
        COMPUTE_STATUS_DISABLED traits to/from the associated resource provider
        in Placement.

        :param context: nova auth RequestContext
        :param enabled: True if the node is enabled in which case the trait
            would be removed, False if the node is disabled in which case
            the trait would be added.
        :raises: ComputeHostNotFound if there are no compute nodes found in
            the ResourceTracker for this service.
        """
        nodes = self.rt.compute_nodes.values()
        if not nodes:
            raise exception.ComputeHostNotFound(host=self.host)
        for node in nodes:
            try:
                self.virtapi.update_compute_provider_status(context, node.uuid, enabled)
            except (exception.ResourceProviderTraitRetrievalFailed, exception.ResourceProviderUpdateConflict, exception.ResourceProviderUpdateFailed, exception.TraitRetrievalFailed) as e:
                LOG.warning('An error occurred while updating COMPUTE_STATUS_DISABLED trait on compute node resource provider %s. The trait will be synchronized when the update_available_resource periodic task runs. Error: %s', node.uuid, e.format_message())
            except Exception:
                LOG.exception('An error occurred while updating COMPUTE_STATUS_DISABLED trait on compute node resource provider %s. The trait will be synchronized when the update_available_resource periodic task runs.', node.uuid)

    @wrap_exception()
    def set_host_enabled(self, context, enabled):
        """Sets the specified host's ability to accept new instances.

        This method will add or remove the COMPUTE_STATUS_DISABLED trait
        to/from the associated compute node resource provider(s) for this
        compute service.
        """
        try:
            self._update_compute_provider_status(context, enabled)
        except exception.ComputeHostNotFound:
            LOG.warning('Unable to add/remove trait COMPUTE_STATUS_DISABLED. No ComputeNode(s) found for host: %s', self.host)
        try:
            return self.driver.set_host_enabled(enabled)
        except NotImplementedError:
            return 'enabled' if enabled else 'disabled'

    @wrap_exception()
    def get_host_uptime(self, context):
        """Returns the result of calling "uptime" on the target host."""
        return self.driver.get_host_uptime()

    @wrap_exception()
    @wrap_instance_fault
    def get_diagnostics(self, context, instance):
        """Retrieve diagnostics for an instance on this host."""
        current_power_state = self._get_power_state(instance)
        if current_power_state == power_state.RUNNING:
            LOG.info('Retrieving diagnostics', instance=instance)
            return self.driver.get_diagnostics(instance)
        else:
            raise exception.InstanceInvalidState(attr='power state', instance_uuid=instance.uuid, state=power_state.STATE_MAP[instance.power_state], method='get_diagnostics')

    @wrap_exception()
    @wrap_instance_fault
    def get_instance_diagnostics(self, context, instance):
        """Retrieve diagnostics for an instance on this host."""
        current_power_state = self._get_power_state(instance)
        if current_power_state == power_state.RUNNING:
            LOG.info('Retrieving diagnostics', instance=instance)
            return self.driver.get_instance_diagnostics(instance)
        else:
            raise exception.InstanceInvalidState(attr='power state', instance_uuid=instance.uuid, state=power_state.STATE_MAP[instance.power_state], method='get_diagnostics')

    @wrap_exception()
    @reverts_task_state
    @wrap_instance_event(prefix='compute')
    @wrap_instance_fault
    def suspend_instance(self, context, instance):
        """Suspend the given instance."""
        context = context.elevated()
        instance.system_metadata['old_vm_state'] = instance.vm_state
        self._notify_about_instance_usage(context, instance, 'suspend.start')
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.SUSPEND, phase=fields.NotificationPhase.START)
        with self._error_out_instance_on_exception(context, instance, instance_state=instance.vm_state):
            self.driver.suspend(context, instance)
        instance.power_state = self._get_power_state(instance)
        instance.vm_state = vm_states.SUSPENDED
        instance.task_state = None
        instance.save(expected_task_state=task_states.SUSPENDING)
        self._notify_about_instance_usage(context, instance, 'suspend.end')
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.SUSPEND, phase=fields.NotificationPhase.END)

    @wrap_exception()
    @reverts_task_state
    @wrap_instance_event(prefix='compute')
    @wrap_instance_fault
    def resume_instance(self, context, instance):
        """Resume the given suspended instance."""
        context = context.elevated()
        LOG.info('Resuming', instance=instance)
        self._notify_about_instance_usage(context, instance, 'resume.start')
        bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(context, instance.uuid)
        block_device_info = self._get_instance_block_device_info(context, instance, bdms=bdms)
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.RESUME, phase=fields.NotificationPhase.START, bdms=bdms)
        network_info = self.network_api.get_instance_nw_info(context, instance)
        with self._error_out_instance_on_exception(context, instance, instance_state=instance.vm_state):
            self.driver.resume(context, instance, network_info, block_device_info)
        instance.power_state = self._get_power_state(instance)
        instance.vm_state = instance.system_metadata.pop('old_vm_state', vm_states.ACTIVE)
        instance.task_state = None
        instance.save(expected_task_state=task_states.RESUMING)
        self._notify_about_instance_usage(context, instance, 'resume.end')
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.RESUME, phase=fields.NotificationPhase.END, bdms=bdms)

    @wrap_exception()
    @reverts_task_state
    @wrap_instance_event(prefix='compute')
    @wrap_instance_fault
    def shelve_instance(self, context, instance, image_id, clean_shutdown, accel_uuids):
        """Shelve an instance.

        This should be used when you want to take a snapshot of the instance.
        It also adds system_metadata that can be used by a periodic task to
        offload the shelved instance after a period of time.

        :param context: request context
        :param instance: an Instance object
        :param image_id: an image id to snapshot to.
        :param clean_shutdown: give the GuestOS a chance to stop
        :param accel_uuids: the accelerators uuids for the instance
        """

        @utils.synchronized(instance.uuid)
        def do_shelve_instance():
            self._shelve_instance(context, instance, image_id, clean_shutdown, accel_uuids)
        do_shelve_instance()

    def _shelve_instance(self, context, instance, image_id, clean_shutdown, accel_uuids=None):
        LOG.info('Shelving', instance=instance)
        offload = CONF.shelved_offload_time == 0
        if offload:
            bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(context, instance.uuid)
        else:
            bdms = None
        compute_utils.notify_usage_exists(self.notifier, context, instance, self.host, current_period=True)
        self._notify_about_instance_usage(context, instance, 'shelve.start')
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.SHELVE, phase=fields.NotificationPhase.START, bdms=bdms)

        def update_task_state(task_state, expected_state=task_states.SHELVING):
            shelving_state_map = {task_states.IMAGE_PENDING_UPLOAD: task_states.SHELVING_IMAGE_PENDING_UPLOAD, task_states.IMAGE_UPLOADING: task_states.SHELVING_IMAGE_UPLOADING, task_states.SHELVING: task_states.SHELVING}
            task_state = shelving_state_map[task_state]
            expected_state = shelving_state_map[expected_state]
            instance.task_state = task_state
            instance.save(expected_task_state=expected_state)
        if instance.power_state == power_state.PAUSED:
            clean_shutdown = False
        self._power_off_instance(instance, clean_shutdown)
        self.driver.snapshot(context, instance, image_id, update_task_state)
        instance.system_metadata['shelved_at'] = timeutils.utcnow().isoformat()
        instance.system_metadata['shelved_image_id'] = image_id
        instance.system_metadata['shelved_host'] = self.host
        instance.vm_state = vm_states.SHELVED
        instance.task_state = None
        if offload:
            instance.task_state = task_states.SHELVING_OFFLOADING
        instance.power_state = self._get_power_state(instance)
        instance.save(expected_task_state=[task_states.SHELVING, task_states.SHELVING_IMAGE_UPLOADING])
        self._notify_about_instance_usage(context, instance, 'shelve.end')
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.SHELVE, phase=fields.NotificationPhase.END, bdms=bdms)
        if offload:
            self._shelve_offload_instance(context, instance, clean_shutdown=False, bdms=bdms, accel_uuids=accel_uuids)

    @wrap_exception()
    @reverts_task_state
    @wrap_instance_event(prefix='compute')
    @wrap_instance_fault
    def shelve_offload_instance(self, context, instance, clean_shutdown, accel_uuids):
        """Remove a shelved instance from the hypervisor.

        This frees up those resources for use by other instances, but may lead
        to slower unshelve times for this instance.  This method is used by
        volume backed instances since restoring them doesn't involve the
        potentially large download of an image.

        :param context: request context
        :param instance: nova.objects.instance.Instance
        :param clean_shutdown: give the GuestOS a chance to stop
        :param accel_uuids: the accelerators uuids for the instance
        """

        @utils.synchronized(instance.uuid)
        def do_shelve_offload_instance():
            self._shelve_offload_instance(context, instance, clean_shutdown, accel_uuids=accel_uuids)
        do_shelve_offload_instance()

    def _shelve_offload_instance(self, context, instance, clean_shutdown, bdms=None, accel_uuids=None):
        LOG.info('Shelve offloading', instance=instance)
        if bdms is None:
            bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(context, instance.uuid)
        self._notify_about_instance_usage(context, instance, 'shelve_offload.start')
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.SHELVE_OFFLOAD, phase=fields.NotificationPhase.START, bdms=bdms)
        self._power_off_instance(instance, clean_shutdown)
        current_power_state = self._get_power_state(instance)
        network_info = self.network_api.get_instance_nw_info(context, instance)
        block_device_info = self._get_instance_block_device_info(context, instance, bdms=bdms)
        self.driver.destroy(context, instance, network_info, block_device_info)
        self._terminate_volume_connections(context, instance, bdms)
        if accel_uuids:
            cyclient = cyborg.get_client(context)
            cyclient.delete_arqs_for_instance(instance.uuid)
        self.rt.delete_allocation_for_shelve_offloaded_instance(context, instance)
        instance.power_state = current_power_state
        instance.vm_state = vm_states.SHELVED_OFFLOADED
        instance.task_state = None
        instance.save(expected_task_state=[task_states.SHELVING, task_states.SHELVING_OFFLOADING])
        self._update_resource_tracker(context, instance)
        self._nil_out_instance_obj_host_and_node(instance)
        instance.save(expected_task_state=None)
        self._delete_scheduler_instance_info(context, instance.uuid)
        self._notify_about_instance_usage(context, instance, 'shelve_offload.end')
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.SHELVE_OFFLOAD, phase=fields.NotificationPhase.END, bdms=bdms)

    @wrap_exception()
    @reverts_task_state
    @wrap_instance_event(prefix='compute')
    @wrap_instance_fault
    def unshelve_instance(self, context, instance, image, filter_properties, node, request_spec, accel_uuids):
        """Unshelve the instance.

        :param context: request context
        :param instance: a nova.objects.instance.Instance object
        :param image: an image to build from.  If None we assume a
            volume backed instance.
        :param filter_properties: dict containing limits, retry info etc.
        :param node: target compute node
        :param request_spec: the RequestSpec object used to schedule the
            instance
        :param accel_uuids: the accelerators uuids for the instance
        """
        if filter_properties is None:
            filter_properties = {}

        @utils.synchronized(instance.uuid)
        def do_unshelve_instance():
            self._unshelve_instance(context, instance, image, filter_properties, node, request_spec, accel_uuids)
        do_unshelve_instance()

    def _unshelve_instance_key_scrub(self, instance):
        """Remove data from the instance that may cause side effects."""
        cleaned_keys = dict(key_data=instance.key_data, auto_disk_config=instance.auto_disk_config)
        instance.key_data = None
        instance.auto_disk_config = False
        return cleaned_keys

    def _unshelve_instance_key_restore(self, instance, keys):
        """Restore previously scrubbed keys before saving the instance."""
        instance.update(keys)

    def _unshelve_instance(self, context, instance, image, filter_properties, node, request_spec, accel_uuids):
        LOG.info('Unshelving', instance=instance)
        bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(context, instance.uuid)
        self._notify_about_instance_usage(context, instance, 'unshelve.start')
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.UNSHELVE, phase=fields.NotificationPhase.START, bdms=bdms)
        instance.task_state = task_states.SPAWNING
        instance.save()
        block_device_info = self._prep_block_device(context, instance, bdms)
        scrubbed_keys = self._unshelve_instance_key_scrub(instance)
        if node is None:
            node = self._get_nodename(instance)
        limits = filter_properties.get('limits', {})
        allocations = self.reportclient.get_allocations_for_consumer(context, instance.uuid)
        shelved_image_ref = instance.image_ref
        if image:
            instance.image_ref = image['id']
            image_meta = objects.ImageMeta.from_dict(image)
        else:
            image_meta = objects.ImageMeta.from_dict(utils.get_image_from_system_metadata(instance.system_metadata))
        provider_mappings = self._get_request_group_mapping(request_spec)
        try:
            if provider_mappings:
                update = compute_utils.update_pci_request_spec_with_allocated_interface_name
                update(context, self.reportclient, instance.pci_requests.requests, provider_mappings)
            accel_info = []
            if accel_uuids:
                try:
                    accel_info = self._get_bound_arq_resources(context, instance, accel_uuids)
                except (Exception, eventlet.timeout.Timeout) as exc:
                    LOG.exception('Failure getting accelerator requests with the exception: %s', exc, instance=instance)
                    self._build_resources_cleanup(instance, None)
                    raise
            with self.rt.instance_claim(context, instance, node, allocations, limits):
                self.network_api.setup_instance_network_on_host(context, instance, self.host, provider_mappings=provider_mappings)
                network_info = self.network_api.get_instance_nw_info(context, instance)
                self.driver.spawn(context, instance, image_meta, injected_files=[], admin_password=None, allocations=allocations, network_info=network_info, block_device_info=block_device_info, accel_info=accel_info)
        except Exception:
            with excutils.save_and_reraise_exception(logger=LOG):
                LOG.exception('Instance failed to spawn', instance=instance)
                self.reportclient.delete_allocation_for_instance(context, instance.uuid)
                self._terminate_volume_connections(context, instance, bdms)
                self._nil_out_instance_obj_host_and_node(instance)
        if image:
            instance.image_ref = shelved_image_ref
            self._delete_snapshot_of_shelved_instance(context, instance, image['id'])
        self._unshelve_instance_key_restore(instance, scrubbed_keys)
        self._update_instance_after_spawn(instance)
        compute_utils.remove_shelved_keys_from_system_metadata(instance)
        instance.save(expected_task_state=task_states.SPAWNING)
        self._update_scheduler_instance_info(context, instance)
        self._notify_about_instance_usage(context, instance, 'unshelve.end')
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.UNSHELVE, phase=fields.NotificationPhase.END, bdms=bdms)

    def _inject_network_info(self, instance, network_info):
        """Inject network info for the given instance."""
        LOG.debug('Inject network info', instance=instance)
        LOG.debug('network_info to inject: |%s|', network_info, instance=instance)
        self.driver.inject_network_info(instance, network_info)

    @wrap_instance_fault
    def inject_network_info(self, context, instance):
        """Inject network info, but don't return the info."""
        network_info = self.network_api.get_instance_nw_info(context, instance)
        self._inject_network_info(instance, network_info)

    @messaging.expected_exceptions(NotImplementedError, exception.ConsoleNotAvailable, exception.InstanceNotFound)
    @wrap_exception()
    @wrap_instance_fault
    def get_console_output(self, context, instance, tail_length):
        """Send the console output for the given instance."""
        context = context.elevated()
        LOG.info('Get console output', instance=instance)
        output = self.driver.get_console_output(context, instance)
        if type(output) is str:
            output = output.encode('latin-1')
        if tail_length is not None:
            output = self._tail_log(output, tail_length)
        return output.decode('ascii', 'replace')

    def _tail_log(self, log, length):
        try:
            length = int(length)
        except ValueError:
            length = 0
        if length == 0:
            return b''
        else:
            return b'\n'.join(log.split(b'\n')[-int(length):])

    @messaging.expected_exceptions(exception.ConsoleTypeInvalid, exception.InstanceNotReady, exception.InstanceNotFound, exception.ConsoleTypeUnavailable, NotImplementedError)
    @wrap_exception()
    @wrap_instance_fault
    def get_vnc_console(self, context, console_type, instance):
        """Return connection information for a vnc console."""
        context = context.elevated()
        LOG.debug('Getting vnc console', instance=instance)
        if not CONF.vnc.enabled:
            raise exception.ConsoleTypeUnavailable(console_type=console_type)
        if console_type == 'novnc':
            access_url_base = CONF.vnc.novncproxy_base_url
        else:
            raise exception.ConsoleTypeInvalid(console_type=console_type)
        try:
            console = self.driver.get_vnc_console(context, instance)
            console_auth = objects.ConsoleAuthToken(context=context, console_type=console_type, host=console.host, port=console.port, internal_access_path=console.internal_access_path, instance_uuid=instance.uuid, access_url_base=access_url_base)
            console_auth.authorize(CONF.consoleauth.token_ttl)
            connect_info = console.get_connection_info(console_auth.token, console_auth.access_url)
        except exception.InstanceNotFound:
            if instance.vm_state != vm_states.BUILDING:
                raise
            raise exception.InstanceNotReady(instance_id=instance.uuid)
        return connect_info

    @messaging.expected_exceptions(exception.ConsoleTypeInvalid, exception.InstanceNotReady, exception.InstanceNotFound, exception.ConsoleTypeUnavailable, NotImplementedError)
    @wrap_exception()
    @wrap_instance_fault
    def get_spice_console(self, context, console_type, instance):
        """Return connection information for a spice console."""
        context = context.elevated()
        LOG.debug('Getting spice console', instance=instance)
        if not CONF.spice.enabled:
            raise exception.ConsoleTypeUnavailable(console_type=console_type)
        if console_type != 'spice-html5':
            raise exception.ConsoleTypeInvalid(console_type=console_type)
        try:
            console = self.driver.get_spice_console(context, instance)
            console_auth = objects.ConsoleAuthToken(context=context, console_type=console_type, host=console.host, port=console.port, internal_access_path=console.internal_access_path, instance_uuid=instance.uuid, access_url_base=CONF.spice.html5proxy_base_url)
            console_auth.authorize(CONF.consoleauth.token_ttl)
            connect_info = console.get_connection_info(console_auth.token, console_auth.access_url)
        except exception.InstanceNotFound:
            if instance.vm_state != vm_states.BUILDING:
                raise
            raise exception.InstanceNotReady(instance_id=instance.uuid)
        return connect_info

    @messaging.expected_exceptions(exception.ConsoleTypeInvalid, exception.InstanceNotReady, exception.InstanceNotFound, exception.ConsoleTypeUnavailable, NotImplementedError)
    @wrap_exception()
    @wrap_instance_fault
    def get_rdp_console(self, context, console_type, instance):
        """Return connection information for a RDP console."""
        context = context.elevated()
        LOG.debug('Getting RDP console', instance=instance)
        if not CONF.rdp.enabled:
            raise exception.ConsoleTypeUnavailable(console_type=console_type)
        if console_type != 'rdp-html5':
            raise exception.ConsoleTypeInvalid(console_type=console_type)
        try:
            console = self.driver.get_rdp_console(context, instance)
            console_auth = objects.ConsoleAuthToken(context=context, console_type=console_type, host=console.host, port=console.port, internal_access_path=console.internal_access_path, instance_uuid=instance.uuid, access_url_base=CONF.rdp.html5_proxy_base_url)
            console_auth.authorize(CONF.consoleauth.token_ttl)
            connect_info = console.get_connection_info(console_auth.token, console_auth.access_url)
        except exception.InstanceNotFound:
            if instance.vm_state != vm_states.BUILDING:
                raise
            raise exception.InstanceNotReady(instance_id=instance.uuid)
        return connect_info

    @messaging.expected_exceptions(exception.ConsoleTypeInvalid, exception.InstanceNotReady, exception.InstanceNotFound, exception.ConsoleTypeUnavailable, NotImplementedError)
    @wrap_exception()
    @wrap_instance_fault
    def get_mks_console(self, context, console_type, instance):
        """Return connection information for a MKS console."""
        context = context.elevated()
        LOG.debug('Getting MKS console', instance=instance)
        if not CONF.mks.enabled:
            raise exception.ConsoleTypeUnavailable(console_type=console_type)
        if console_type != 'webmks':
            raise exception.ConsoleTypeInvalid(console_type=console_type)
        try:
            console = self.driver.get_mks_console(context, instance)
            console_auth = objects.ConsoleAuthToken(context=context, console_type=console_type, host=console.host, port=console.port, internal_access_path=console.internal_access_path, instance_uuid=instance.uuid, access_url_base=CONF.mks.mksproxy_base_url)
            console_auth.authorize(CONF.consoleauth.token_ttl)
            connect_info = console.get_connection_info(console_auth.token, console_auth.access_url)
        except exception.InstanceNotFound:
            if instance.vm_state != vm_states.BUILDING:
                raise
            raise exception.InstanceNotReady(instance_id=instance.uuid)
        return connect_info

    @messaging.expected_exceptions(exception.ConsoleTypeInvalid, exception.InstanceNotReady, exception.InstanceNotFound, exception.ConsoleTypeUnavailable, exception.SocketPortRangeExhaustedException, exception.ImageSerialPortNumberInvalid, exception.ImageSerialPortNumberExceedFlavorValue, NotImplementedError)
    @wrap_exception()
    @wrap_instance_fault
    def get_serial_console(self, context, console_type, instance):
        """Returns connection information for a serial console."""
        LOG.debug('Getting serial console', instance=instance)
        if not CONF.serial_console.enabled:
            raise exception.ConsoleTypeUnavailable(console_type=console_type)
        context = context.elevated()
        try:
            console = self.driver.get_serial_console(context, instance)
            console_auth = objects.ConsoleAuthToken(context=context, console_type=console_type, host=console.host, port=console.port, internal_access_path=console.internal_access_path, instance_uuid=instance.uuid, access_url_base=CONF.serial_console.base_url)
            console_auth.authorize(CONF.consoleauth.token_ttl)
            connect_info = console.get_connection_info(console_auth.token, console_auth.access_url)
        except exception.InstanceNotFound:
            if instance.vm_state != vm_states.BUILDING:
                raise
            raise exception.InstanceNotReady(instance_id=instance.uuid)
        return connect_info

    @messaging.expected_exceptions(exception.ConsoleTypeInvalid, exception.InstanceNotReady, exception.InstanceNotFound)
    @wrap_exception()
    @wrap_instance_fault
    def validate_console_port(self, ctxt, instance, port, console_type):
        if console_type == 'spice-html5':
            console_info = self.driver.get_spice_console(ctxt, instance)
        elif console_type == 'rdp-html5':
            console_info = self.driver.get_rdp_console(ctxt, instance)
        elif console_type == 'serial':
            console_info = self.driver.get_serial_console(ctxt, instance)
        elif console_type == 'webmks':
            console_info = self.driver.get_mks_console(ctxt, instance)
        else:
            console_info = self.driver.get_vnc_console(ctxt, instance)
        return str(console_info.port) == port

    @wrap_exception()
    @reverts_task_state
    @wrap_instance_fault
    def reserve_block_device_name(self, context, instance, device, volume_id, disk_bus, device_type, tag, multiattach):
        if tag and (not self.driver.capabilities.get('supports_tagged_attach_volume', False)):
            raise exception.VolumeTaggedAttachNotSupported()
        if multiattach and (not self.driver.capabilities.get('supports_multiattach', False)):
            raise exception.MultiattachNotSupportedByVirtDriver(volume_id=volume_id)

        @utils.synchronized(instance.uuid)
        def do_reserve():
            bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(context, instance.uuid)
            new_bdm = objects.BlockDeviceMapping(context=context, source_type='volume', destination_type='volume', instance_uuid=instance.uuid, boot_index=None, volume_id=volume_id, device_name=device, guest_format=None, disk_bus=disk_bus, device_type=device_type, tag=tag)
            new_bdm.device_name = self._get_device_name_for_instance(instance, bdms, new_bdm)
            new_bdm.create()
            return new_bdm
        return do_reserve()

    @wrap_exception()
    @wrap_instance_event(prefix='compute')
    @wrap_instance_fault
    def attach_volume(self, context, instance, bdm):
        """Attach a volume to an instance."""
        driver_bdm = driver_block_device.convert_volume(bdm)

        @utils.synchronized(instance.uuid)
        def do_attach_volume(context, instance, driver_bdm):
            try:
                return self._attach_volume(context, instance, driver_bdm)
            except Exception:
                with excutils.save_and_reraise_exception():
                    bdm.destroy()
        do_attach_volume(context, instance, driver_bdm)

    def _attach_volume(self, context, instance, bdm):
        context = context.elevated()
        LOG.info('Attaching volume %(volume_id)s to %(mountpoint)s', {'volume_id': bdm.volume_id, 'mountpoint': bdm['mount_device']}, instance=instance)
        compute_utils.notify_about_volume_attach_detach(context, instance, self.host, action=fields.NotificationAction.VOLUME_ATTACH, phase=fields.NotificationPhase.START, volume_id=bdm.volume_id)
        try:
            bdm.attach(context, instance, self.volume_api, self.driver, do_driver_attach=True)
        except Exception as e:
            with excutils.save_and_reraise_exception():
                LOG.exception('Failed to attach %(volume_id)s at %(mountpoint)s', {'volume_id': bdm.volume_id, 'mountpoint': bdm['mount_device']}, instance=instance)
                if bdm['attachment_id']:
                    try:
                        self.volume_api.attachment_delete(context, bdm['attachment_id'])
                    except exception.VolumeAttachmentNotFound as exc:
                        LOG.debug('Ignoring VolumeAttachmentNotFound: %s', exc, instance=instance)
                else:
                    self.volume_api.unreserve_volume(context, bdm.volume_id)
                compute_utils.notify_about_volume_attach_detach(context, instance, self.host, action=fields.NotificationAction.VOLUME_ATTACH, phase=fields.NotificationPhase.ERROR, exception=e, volume_id=bdm.volume_id)
        info = {'volume_id': bdm.volume_id}
        self._notify_about_instance_usage(context, instance, 'volume.attach', extra_usage_info=info)
        compute_utils.notify_about_volume_attach_detach(context, instance, self.host, action=fields.NotificationAction.VOLUME_ATTACH, phase=fields.NotificationPhase.END, volume_id=bdm.volume_id)

    def _notify_volume_usage_detach(self, context, instance, bdm):
        if CONF.volume_usage_poll_interval <= 0:
            return
        mp = bdm.device_name
        if '/dev/' in mp:
            mp = mp[5:]
        try:
            vol_stats = self.driver.block_stats(instance, mp)
            if vol_stats is None:
                return
        except NotImplementedError:
            return
        LOG.debug('Updating volume usage cache with totals', instance=instance)
        (rd_req, rd_bytes, wr_req, wr_bytes, flush_ops) = vol_stats
        vol_usage = objects.VolumeUsage(context)
        vol_usage.volume_id = bdm.volume_id
        vol_usage.instance_uuid = instance.uuid
        vol_usage.project_id = instance.project_id
        vol_usage.user_id = instance.user_id
        vol_usage.availability_zone = instance.availability_zone
        vol_usage.curr_reads = rd_req
        vol_usage.curr_read_bytes = rd_bytes
        vol_usage.curr_writes = wr_req
        vol_usage.curr_write_bytes = wr_bytes
        vol_usage.save(update_totals=True)
        self.notifier.info(context, 'volume.usage', vol_usage.to_dict())
        compute_utils.notify_about_volume_usage(context, vol_usage, self.host)

    def _detach_volume(self, context, bdm, instance, destroy_bdm=True, attachment_id=None):
        """Detach a volume from an instance.

        :param context: security context
        :param bdm: nova.objects.BlockDeviceMapping volume bdm to detach
        :param instance: the Instance object to detach the volume from
        :param destroy_bdm: if True, the corresponding BDM entry will be marked
                            as deleted. Disabling this is useful for operations
                            like rebuild, when we don't want to destroy BDM
        :param attachment_id: The volume attachment_id for the given instance
                              and volume.
        """
        volume_id = bdm.volume_id
        compute_utils.notify_about_volume_attach_detach(context, instance, self.host, action=fields.NotificationAction.VOLUME_DETACH, phase=fields.NotificationPhase.START, volume_id=volume_id)
        self._notify_volume_usage_detach(context, instance, bdm)
        LOG.info('Detaching volume %(volume_id)s', {'volume_id': volume_id}, instance=instance)
        driver_bdm = driver_block_device.convert_volume(bdm)
        driver_bdm.detach(context, instance, self.volume_api, self.driver, attachment_id=attachment_id, destroy_bdm=destroy_bdm)
        info = dict(volume_id=volume_id)
        self._notify_about_instance_usage(context, instance, 'volume.detach', extra_usage_info=info)
        compute_utils.notify_about_volume_attach_detach(context, instance, self.host, action=fields.NotificationAction.VOLUME_DETACH, phase=fields.NotificationPhase.END, volume_id=volume_id)
        if 'tag' in bdm and bdm.tag:
            self._delete_disk_metadata(instance, bdm)
        if destroy_bdm:
            bdm.destroy()

    def _delete_disk_metadata(self, instance, bdm):
        for device in instance.device_metadata.devices:
            if isinstance(device, objects.DiskMetadata):
                if 'serial' in device:
                    if device.serial == bdm.volume_id:
                        instance.device_metadata.devices.remove(device)
                        instance.save()
                        break
                else:
                    LOG.warning('Unable to determine whether to clean up device metadata for disk %s', device, instance=instance)

    @wrap_exception()
    @wrap_instance_event(prefix='compute')
    @wrap_instance_fault
    def detach_volume(self, context, volume_id, instance, attachment_id):
        """Detach a volume from an instance.

        :param context: security context
        :param volume_id: the volume id
        :param instance: the Instance object to detach the volume from
        :param attachment_id: The volume attachment_id for the given instance
                              and volume.

        """

        @utils.synchronized(instance.uuid)
        def do_detach_volume(context, volume_id, instance, attachment_id):
            bdm = objects.BlockDeviceMapping.get_by_volume_and_instance(context, volume_id, instance.uuid)
            self._detach_volume(context, bdm, instance, attachment_id=attachment_id)
        do_detach_volume(context, volume_id, instance, attachment_id)

    def _init_volume_connection(self, context, new_volume, old_volume_id, connector, bdm, new_attachment_id, mountpoint):
        new_volume_id = new_volume['id']
        if new_attachment_id is None:
            new_cinfo = self.volume_api.initialize_connection(context, new_volume_id, connector)
        else:
            vol_multiattach = new_volume.get('multiattach', False)
            virt_multiattach = self.driver.capabilities.get('supports_multiattach', False)
            if vol_multiattach and (not virt_multiattach):
                raise exception.MultiattachNotSupportedByVirtDriver(volume_id=new_volume_id)
            new_cinfo = self.volume_api.attachment_update(context, new_attachment_id, connector, mountpoint)['connection_info']
            if vol_multiattach:
                new_cinfo['multiattach'] = True
        old_cinfo = jsonutils.loads(bdm['connection_info'])
        if old_cinfo and 'serial' not in old_cinfo:
            old_cinfo['serial'] = old_volume_id
        if 'serial' not in new_cinfo:
            new_cinfo['serial'] = new_volume_id
        return (old_cinfo, new_cinfo)

    def _swap_volume(self, context, instance, bdm, connector, old_volume_id, new_volume, resize_to, new_attachment_id, is_cinder_migration):
        new_volume_id = new_volume['id']
        mountpoint = bdm['device_name']
        failed = False
        new_cinfo = None
        try:
            (old_cinfo, new_cinfo) = self._init_volume_connection(context, new_volume, old_volume_id, connector, bdm, new_attachment_id, mountpoint)
            msg = 'swap_volume: Calling driver volume swap with connection infos: new: %(new_cinfo)s; old: %(old_cinfo)s' % {'new_cinfo': new_cinfo, 'old_cinfo': old_cinfo}
            LOG.debug(strutils.mask_password(msg), instance=instance)
            self.driver.swap_volume(context, old_cinfo, new_cinfo, instance, mountpoint, resize_to)
            if new_attachment_id:
                self.volume_api.attachment_complete(context, new_attachment_id)
            msg = 'swap_volume: Driver volume swap returned, new connection_info is now : %(new_cinfo)s' % {'new_cinfo': new_cinfo}
            LOG.debug(strutils.mask_password(msg))
        except Exception as ex:
            failed = True
            with excutils.save_and_reraise_exception():
                compute_utils.notify_about_volume_swap(context, instance, self.host, fields.NotificationPhase.ERROR, old_volume_id, new_volume_id, ex)
                if new_cinfo:
                    msg = 'Failed to swap volume %(old_volume_id)s for %(new_volume_id)s'
                    LOG.exception(msg, {'old_volume_id': old_volume_id, 'new_volume_id': new_volume_id}, instance=instance)
                else:
                    msg = 'Failed to connect to volume %(volume_id)s with volume at %(mountpoint)s'
                    LOG.exception(msg, {'volume_id': new_volume_id, 'mountpoint': bdm['device_name']}, instance=instance)
                self.volume_api.roll_detaching(context, old_volume_id)
                if new_attachment_id is None:
                    self.volume_api.unreserve_volume(context, new_volume_id)
                else:
                    self.volume_api.attachment_delete(context, new_attachment_id)
        finally:
            conn_volume = new_volume_id if failed else old_volume_id
            if new_cinfo:
                LOG.debug('swap_volume: removing Cinder connection for volume %(volume)s', {'volume': conn_volume}, instance=instance)
                if bdm.attachment_id is None:
                    self.volume_api.terminate_connection(context, conn_volume, connector)
                elif not failed:
                    self.volume_api.attachment_delete(context, bdm.attachment_id)
            if bdm.attachment_id and (not is_cinder_migration):
                comp_ret = {'save_volume_id': new_volume_id}
            else:
                comp_ret = self.volume_api.migrate_volume_completion(context, old_volume_id, new_volume_id, error=failed)
                LOG.debug('swap_volume: Cinder migrate_volume_completion returned: %(comp_ret)s', {'comp_ret': comp_ret}, instance=instance)
        return (comp_ret, new_cinfo)

    @wrap_exception()
    @wrap_instance_event(prefix='compute')
    @wrap_instance_fault
    def swap_volume(self, context, old_volume_id, new_volume_id, instance, new_attachment_id):
        """Replace the old volume with the new volume within the active server

        :param context: User request context
        :param old_volume_id: Original volume id
        :param new_volume_id: New volume id being swapped to
        :param instance: Instance with original_volume_id attached
        :param new_attachment_id: ID of the new attachment for new_volume_id
        """

        @utils.synchronized(instance.uuid)
        def _do_locked_swap_volume(context, old_volume_id, new_volume_id, instance, new_attachment_id):
            self._do_swap_volume(context, old_volume_id, new_volume_id, instance, new_attachment_id)
        _do_locked_swap_volume(context, old_volume_id, new_volume_id, instance, new_attachment_id)

    def _do_swap_volume(self, context, old_volume_id, new_volume_id, instance, new_attachment_id):
        """Replace the old volume with the new volume within the active server

        :param context: User request context
        :param old_volume_id: Original volume id
        :param new_volume_id: New volume id being swapped to
        :param instance: Instance with original_volume_id attached
        :param new_attachment_id: ID of the new attachment for new_volume_id
        """
        context = context.elevated()
        compute_utils.notify_about_volume_swap(context, instance, self.host, fields.NotificationPhase.START, old_volume_id, new_volume_id)
        bdm = objects.BlockDeviceMapping.get_by_volume_and_instance(context, old_volume_id, instance.uuid)
        connector = self.driver.get_volume_connector(instance)
        resize_to = 0
        old_volume = self.volume_api.get(context, old_volume_id)
        is_cinder_migration = False
        if 'migration_status' in old_volume:
            is_cinder_migration = old_volume['migration_status'] == 'migrating'
        old_vol_size = old_volume['size']
        new_volume = self.volume_api.get(context, new_volume_id)
        new_vol_size = new_volume['size']
        if new_vol_size > old_vol_size:
            resize_to = new_vol_size
        LOG.info('Swapping volume %(old_volume)s for %(new_volume)s', {'old_volume': old_volume_id, 'new_volume': new_volume_id}, instance=instance)
        (comp_ret, new_cinfo) = self._swap_volume(context, instance, bdm, connector, old_volume_id, new_volume, resize_to, new_attachment_id, is_cinder_migration)
        save_volume_id = comp_ret['save_volume_id']
        new_cinfo['serial'] = save_volume_id
        values = {'connection_info': jsonutils.dumps(new_cinfo), 'source_type': 'volume', 'destination_type': 'volume', 'snapshot_id': None, 'volume_id': save_volume_id, 'no_device': None}
        if resize_to:
            values['volume_size'] = resize_to
        if new_attachment_id is not None:
            values['attachment_id'] = new_attachment_id
        LOG.debug('swap_volume: Updating volume %(volume_id)s BDM record with %(updates)s', {'volume_id': bdm.volume_id, 'updates': values}, instance=instance)
        bdm.update(values)
        bdm.save()
        compute_utils.notify_about_volume_swap(context, instance, self.host, fields.NotificationPhase.END, old_volume_id, new_volume_id)

    @wrap_exception()
    def remove_volume_connection(self, context, volume_id, instance):
        """Remove the volume connection on this host

        Detach the volume from this instance on this host, and if this is
        the cinder v2 flow, call cinder to terminate the connection.
        """
        try:
            bdm = objects.BlockDeviceMapping.get_by_volume_and_instance(context, volume_id, instance.uuid)
            self._remove_volume_connection(context, bdm, instance)
        except exception.NotFound:
            pass

    def _remove_volume_connection(self, context, bdm, instance, delete_attachment=False):
        """Remove the volume connection on this host

        Detach the volume from this instance on this host.

        :param context: nova auth request context
        :param bdm: BlockDeviceMapping object for a volume attached to the
            instance
        :param instance: Instance object with a volume attached represented
            by ``bdm``
        :param delete_attachment: If ``bdm.attachment_id`` is not None the
            attachment was made as a cinder v3 style attachment and if True,
            then deletes the volume attachment, otherwise just terminates
            the connection for a cinder legacy style connection.
        """
        driver_bdm = driver_block_device.convert_volume(bdm)
        driver_bdm.driver_detach(context, instance, self.volume_api, self.driver)
        if bdm.attachment_id is None:
            connector = self.driver.get_volume_connector(instance)
            self.volume_api.terminate_connection(context, bdm.volume_id, connector)
        elif delete_attachment:
            self.volume_api.attachment_delete(context, bdm.attachment_id)

    def _deallocate_port_resource_for_instance(self, context: nova.context.RequestContext, instance: 'objects.Instance', port_id: str, port_allocation: ty.Dict[str, ty.Dict[str, ty.Dict[str, int]]]) -> None:
        if not port_allocation:
            return
        try:
            client = self.reportclient
            client.remove_resources_from_instance_allocation(context, instance.uuid, port_allocation)
        except Exception as ex:
            with excutils.save_and_reraise_exception():
                LOG.warning('Failed to remove resource allocation of port %(port_id)s for instance. Error: %(error)s', {'port_id': port_id, 'error': ex}, instance=instance)

    def _deallocate_port_for_instance(self, context, instance, port_id, raise_on_failure=False, pci_device=None):
        try:
            result = self.network_api.deallocate_port_for_instance(context, instance, port_id)
            (__, port_allocation) = result
        except Exception as ex:
            with excutils.save_and_reraise_exception(reraise=raise_on_failure):
                LOG.warning('Failed to deallocate port %(port_id)s for instance. Error: %(error)s', {'port_id': port_id, 'error': ex}, instance=instance)
        else:
            if pci_device:
                self.rt.unclaim_pci_devices(context, pci_device, instance)
                instance.remove_pci_device_and_request(pci_device)
            self._deallocate_port_resource_for_instance(context, instance, port_id, port_allocation)

    def _claim_pci_device_for_interface_attach(self, context: nova.context.RequestContext, instance: 'objects.Instance', pci_reqs: 'objects.InstancePCIRequests') -> ty.Optional['objects.PciDevice']:
        """Claim PCI devices if there are PCI requests

        :param context: nova.context.RequestContext
        :param instance: the objects.Instance to where the interface is being
            attached
        :param pci_reqs: A InstancePCIRequests object describing the
            needed PCI devices
        :raises InterfaceAttachPciClaimFailed: if the PCI device claim fails
        :returns: An objects.PciDevice describing the claimed PCI device for
            the interface or None if no device is requested
        """
        if not pci_reqs.requests:
            return None
        devices = self.rt.claim_pci_devices(context, pci_reqs, instance.numa_topology)
        if not devices:
            LOG.info('Failed to claim PCI devices during interface attach for PCI request %s', pci_reqs, instance=instance)
            raise exception.InterfaceAttachPciClaimFailed(instance_uuid=instance.uuid)
        device = devices[0]
        instance.pci_devices.objects.append(device)
        return device

    def _allocate_port_resource_for_instance(self, context: nova.context.RequestContext, instance: 'objects.Instance', pci_reqs: 'objects.InstancePCIRequests', request_groups: ty.List['objects.RequestGroup']) -> ty.Tuple[ty.Optional[ty.Dict[str, ty.List[str]]], ty.Optional[ty.Dict[str, ty.Dict[str, ty.Dict[str, int]]]]]:
        """Allocate resources for the request in placement

        :param context: nova.context.RequestContext
        :param instance: the objects.Instance to where the interface is being
            attached
        :param pci_reqs: A list of InstancePCIRequest objects describing the
            needed PCI devices
        :param request_groups: A list of RequestGroup objects describing the
            resources the port requests from placement
        :raises InterfaceAttachResourceAllocationFailed: if we failed to
            allocate resource in placement for the request
        :returns: A tuple of provider mappings and allocated resources or
            (None, None) if no resource allocation was needed for the request
        """
        if not request_groups:
            return (None, None)
        request_group = request_groups[0]
        compute_node_uuid = objects.ComputeNode.get_by_nodename(context, instance.node).uuid
        request_group.in_tree = compute_node_uuid
        rr = scheduler_utils.ResourceRequest.from_request_group(request_group)
        res = self.reportclient.get_allocation_candidates(context, rr)
        (alloc_reqs, provider_sums, version) = res
        if not alloc_reqs:
            raise exception.InterfaceAttachResourceAllocationFailed(instance_uuid=instance.uuid)
        alloc_req = alloc_reqs[0]
        resources = alloc_req['allocations']
        provider_mappings = alloc_req['mappings']
        try:
            self.reportclient.add_resources_to_instance_allocation(context, instance.uuid, resources)
        except exception.AllocationUpdateFailed as e:
            raise exception.InterfaceAttachResourceAllocationFailed(instance_uuid=instance.uuid) from e
        except (exception.ConsumerAllocationRetrievalFailed, keystone_exception.ClientException) as e:
            raise exception.InterfaceAttachResourceAllocationFailed(instance_uuid=instance.uuid) from e
        try:
            update = compute_utils.update_pci_request_spec_with_allocated_interface_name
            update(context, self.reportclient, pci_reqs.requests, provider_mappings)
        except (exception.AmbiguousResourceProviderForPCIRequest, exception.UnexpectedResourceProviderNameForPCIRequest):
            with excutils.save_and_reraise_exception():
                self.reportclient.remove_resources_from_instance_allocation(context, instance.uuid, resources)
        return (provider_mappings, resources)

    @messaging.expected_exceptions(exception.Invalid)
    @wrap_exception()
    @wrap_instance_event(prefix='compute')
    @wrap_instance_fault
    def attach_interface(self, context, instance, network_id, port_id, requested_ip, tag):
        """Use hotplug to add an network adapter to an instance."""
        lockname = 'interface-%s-%s' % (instance.uuid, port_id)

        @utils.synchronized(lockname)
        def do_attach_interface(context, instance, network_id, port_id, requested_ip, tag):
            return self._attach_interface(context, instance, network_id, port_id, requested_ip, tag)
        return do_attach_interface(context, instance, network_id, port_id, requested_ip, tag)

    def _attach_interface(self, context, instance, network_id, port_id, requested_ip, tag):
        if not self.driver.capabilities.get('supports_attach_interface', False):
            raise exception.AttachInterfaceNotSupported(instance_uuid=instance.uuid)
        if tag and (not self.driver.capabilities.get('supports_tagged_attach_interface', False)):
            raise exception.NetworkInterfaceTaggedAttachNotSupported()
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.INTERFACE_ATTACH, phase=fields.NotificationPhase.START)
        bind_host_id = self.driver.network_binding_host_id(context, instance)
        requested_networks = objects.NetworkRequestList(objects=[objects.NetworkRequest(network_id=network_id, port_id=port_id, address=requested_ip, tag=tag)])
        if len(requested_networks) != 1:
            LOG.warning('Interface attach only supports one interface per attach request', instance=instance)
            raise exception.InterfaceAttachFailed(instance_uuid=instance.uuid)
        pci_numa_affinity_policy = hardware.get_pci_numa_policy_constraint(instance.flavor, instance.image_meta)
        pci_reqs = objects.InstancePCIRequests(requests=[], instance_uuid=instance.uuid)
        (_, request_groups) = self.network_api.create_resource_requests(context, requested_networks, pci_reqs, affinity_policy=pci_numa_affinity_policy)
        if pci_reqs.requests:
            pci_req = pci_reqs.requests[0]
            requested_networks[0].pci_request_id = pci_req.request_id
        result = self._allocate_port_resource_for_instance(context, instance, pci_reqs, request_groups)
        (provider_mappings, resources) = result
        try:
            pci_device = self._claim_pci_device_for_interface_attach(context, instance, pci_reqs)
        except exception.InterfaceAttachPciClaimFailed:
            with excutils.save_and_reraise_exception():
                if resources:
                    self._deallocate_port_resource_for_instance(context, instance, port_id, resources)
        instance.pci_requests.requests.extend(pci_reqs.requests)
        network_info = self.network_api.allocate_for_instance(context, instance, requested_networks, bind_host_id=bind_host_id, resource_provider_mapping=provider_mappings)
        if len(network_info) != 1:
            LOG.error('allocate_for_instance returned %(ports)s ports', {'ports': len(network_info)})
            raise exception.InterfaceAttachFailed(instance_uuid=instance.uuid)
        image_meta = objects.ImageMeta.from_instance(instance)
        try:
            self.driver.attach_interface(context, instance, image_meta, network_info[0])
        except exception.NovaException as ex:
            port_id = network_info[0].get('id')
            LOG.warning('attach interface failed , try to deallocate port %(port_id)s, reason: %(msg)s', {'port_id': port_id, 'msg': ex}, instance=instance)
            self._deallocate_port_for_instance(context, instance, port_id, pci_device=pci_device)
            compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.INTERFACE_ATTACH, phase=fields.NotificationPhase.ERROR, exception=ex)
            raise exception.InterfaceAttachFailed(instance_uuid=instance.uuid)
        if pci_device:
            self.rt.allocate_pci_devices_for_instance(context, instance)
        instance.save()
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.INTERFACE_ATTACH, phase=fields.NotificationPhase.END)
        return network_info[0]

    @wrap_exception()
    @wrap_instance_event(prefix='compute')
    @wrap_instance_fault
    def detach_interface(self, context, instance, port_id):
        """Detach a network adapter from an instance."""
        lockname = 'interface-%s-%s' % (instance.uuid, port_id)

        @utils.synchronized(lockname)
        def do_detach_interface(context, instance, port_id):
            self._detach_interface(context, instance, port_id)
        do_detach_interface(context, instance, port_id)

    def _detach_interface(self, context, instance, port_id):
        compute_utils.refresh_info_cache_for_instance(context, instance)
        network_info = instance.info_cache.network_info
        condemned = None
        for vif in network_info:
            if vif['id'] == port_id:
                condemned = vif
                break
        if condemned is None:
            raise exception.PortNotFound(_('Port %s is not attached') % port_id)
        pci_req = pci_req_module.get_instance_pci_request_from_vif(context, instance, condemned)
        pci_device = None
        if pci_req:
            pci_devices = [pci_device for pci_device in instance.pci_devices.objects if pci_device.request_id == pci_req.request_id]
            if not pci_devices:
                LOG.warning('Detach interface failed, port_id=%(port_id)s, reason: PCI device not found for PCI request %(pci_req)s', {'port_id': port_id, 'pci_req': pci_req})
                raise exception.InterfaceDetachFailed(instance_uuid=instance.uuid)
            pci_device = pci_devices[0]
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.INTERFACE_DETACH, phase=fields.NotificationPhase.START)
        try:
            self.driver.detach_interface(context, instance, condemned)
        except exception.NovaException as ex:
            log_level = logging.DEBUG if isinstance(ex, exception.InstanceNotFound) else logging.WARNING
            LOG.log(log_level, 'Detach interface failed, port_id=%(port_id)s, reason: %(msg)s', {'port_id': port_id, 'msg': ex}, instance=instance)
            raise exception.InterfaceDetachFailed(instance_uuid=instance.uuid)
        else:
            self._deallocate_port_for_instance(context, instance, port_id, raise_on_failure=True, pci_device=pci_device)
        instance.save()
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.INTERFACE_DETACH, phase=fields.NotificationPhase.END)

    def _get_compute_info(self, context, host):
        return objects.ComputeNode.get_first_node_by_host_for_old_compat(context, host)

    @wrap_exception()
    def check_instance_shared_storage(self, ctxt, data):
        """Check if the instance files are shared

        :param ctxt: security context
        :param data: result of driver.check_instance_shared_storage_local

        Returns True if instance disks located on shared storage and
        False otherwise.
        """
        return self.driver.check_instance_shared_storage_remote(ctxt, data)

    def _dest_can_numa_live_migrate(self, dest_check_data, migration):
        if 'dst_supports_numa_live_migration' in dest_check_data and dest_check_data.dst_supports_numa_live_migration and (not migration):
            delattr(dest_check_data, 'dst_supports_numa_live_migration')
        return dest_check_data

    @wrap_exception()
    @wrap_instance_event(prefix='compute')
    @wrap_instance_fault
    def check_can_live_migrate_destination(self, ctxt, instance, block_migration, disk_over_commit, migration, limits):
        """Check if it is possible to execute live migration.

        This runs checks on the destination host, and then calls
        back to the source host to check the results.

        :param context: security context
        :param instance: dict of instance data
        :param block_migration: if true, prepare for block migration
                                if None, calculate it in driver
        :param disk_over_commit: if true, allow disk over commit
                                 if None, ignore disk usage checking
        :param migration: objects.Migration object for this live migration.
        :param limits: objects.SchedulerLimits object for this live migration.
        :returns: a LiveMigrateData object (hypervisor-dependent)
        """
        try:
            self._validate_instance_group_policy(ctxt, instance)
        except exception.RescheduledException as e:
            msg = 'Failed to validate instance group policy due to: {}'.format(e)
            raise exception.MigrationPreCheckError(reason=msg)
        src_compute_info = obj_base.obj_to_primitive(self._get_compute_info(ctxt, instance.host))
        dst_compute_info = obj_base.obj_to_primitive(self._get_compute_info(ctxt, self.host))
        dest_check_data = self.driver.check_can_live_migrate_destination(ctxt, instance, src_compute_info, dst_compute_info, block_migration, disk_over_commit)
        dest_check_data = self._dest_can_numa_live_migrate(dest_check_data, migration)
        LOG.debug('destination check data is %s', dest_check_data)
        try:
            allocs = self.reportclient.get_allocations_for_consumer(ctxt, instance.uuid)
            migrate_data = self.compute_rpcapi.check_can_live_migrate_source(ctxt, instance, dest_check_data)
            if 'src_supports_numa_live_migration' in migrate_data and migrate_data.src_supports_numa_live_migration:
                migrate_data = self._live_migration_claim(ctxt, instance, migrate_data, migration, limits, allocs)
            elif 'dst_supports_numa_live_migration' in dest_check_data:
                LOG.info('Destination was ready for NUMA live migration, but source is either too old, or is set to an older upgrade level.', instance=instance)
            if self.network_api.supports_port_binding_extension(ctxt):
                migrate_data.vifs = migrate_data_obj.VIFMigrateData.create_skeleton_migrate_vifs(instance.get_network_info())
                port_id_to_pci = self._claim_pci_for_instance_vifs(ctxt, instance)
                self._update_migrate_vifs_profile_with_pci(migrate_data.vifs, port_id_to_pci)
        finally:
            self.driver.cleanup_live_migration_destination_check(ctxt, dest_check_data)
        return migrate_data

    def _live_migration_claim(self, ctxt, instance, migrate_data, migration, limits, allocs):
        """Runs on the destination and does a resources claim, if necessary.
        Currently, only NUMA live migrations require it.

        :param ctxt: Request context
        :param instance: The Instance being live migrated
        :param migrate_data: The MigrateData object for this live migration
        :param migration: The Migration object for this live migration
        :param limits: The SchedulerLimits object for this live migration
        :returns: migrate_data with dst_numa_info set if necessary
        """
        try:
            claim = self.rt.live_migration_claim(ctxt, instance, self._get_nodename(instance), migration, limits, allocs)
            LOG.debug('Created live migration claim.', instance=instance)
        except exception.ComputeResourcesUnavailable as e:
            raise exception.MigrationPreCheckError(reason=e.format_message())
        return self.driver.post_claim_migrate_data(ctxt, instance, migrate_data, claim)

    def _source_can_numa_live_migrate(self, ctxt, dest_check_data, source_check_data):
        can_numa_live_migrate = 'dst_supports_numa_live_migration' in dest_check_data and dest_check_data.dst_supports_numa_live_migration and self.compute_rpcapi.supports_numa_live_migration(ctxt)
        if 'src_supports_numa_live_migration' in source_check_data and source_check_data.src_supports_numa_live_migration and (not can_numa_live_migrate):
            delattr(source_check_data, 'src_supports_numa_live_migration')
        return source_check_data

    @wrap_exception()
    @wrap_instance_event(prefix='compute')
    @wrap_instance_fault
    def check_can_live_migrate_source(self, ctxt, instance, dest_check_data):
        """Check if it is possible to execute live migration.

        This checks if the live migration can succeed, based on the
        results from check_can_live_migrate_destination.

        :param ctxt: security context
        :param instance: dict of instance data
        :param dest_check_data: result of check_can_live_migrate_destination
        :returns: a LiveMigrateData object
        """
        bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(ctxt, instance.uuid)
        is_volume_backed = compute_utils.is_volume_backed_instance(ctxt, instance, bdms)
        dest_check_data.is_volume_backed = is_volume_backed
        block_device_info = self._get_instance_block_device_info(ctxt, instance, refresh_conn_info=False, bdms=bdms)
        result = self.driver.check_can_live_migrate_source(ctxt, instance, dest_check_data, block_device_info)
        result = self._source_can_numa_live_migrate(ctxt, dest_check_data, result)
        LOG.debug('source check data is %s', result)
        return result

    @wrap_exception()
    @wrap_instance_event(prefix='compute')
    @wrap_instance_fault
    def pre_live_migration(self, context, instance, disk, migrate_data):
        """Preparations for live migration at dest host.

        :param context: security context
        :param instance: dict of instance data
        :param disk: disk info of instance
        :param migrate_data: A dict or LiveMigrateData object holding data
                             required for live migration without shared
                             storage.
        :returns: migrate_data containing additional migration info
        """
        LOG.debug('pre_live_migration data is %s', migrate_data)
        self._validate_instance_group_policy(context, instance)
        migrate_data.old_vol_attachment_ids = {}
        bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(context, instance.uuid)
        network_info = self.network_api.get_instance_nw_info(context, instance)
        self._notify_about_instance_usage(context, instance, 'live_migration.pre.start', network_info=network_info)
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.LIVE_MIGRATION_PRE, phase=fields.NotificationPhase.START, bdms=bdms)
        connector = self.driver.get_volume_connector(instance)
        try:
            for bdm in bdms:
                if bdm.is_volume and bdm.attachment_id is not None:
                    attach_ref = self.volume_api.attachment_create(context, bdm.volume_id, bdm.instance_uuid, connector=connector, mountpoint=bdm.device_name)
                    migrate_data.old_vol_attachment_ids[bdm.volume_id] = bdm.attachment_id
                    bdm.attachment_id = attach_ref['id']
                    bdm.save()
            block_device_info = self._get_instance_block_device_info(context, instance, refresh_conn_info=True, bdms=bdms)
            migrate_data = self.driver.pre_live_migration(context, instance, block_device_info, network_info, disk, migrate_data)
            LOG.debug('driver pre_live_migration data is %s', migrate_data)
            using_multiple_port_bindings = 'vifs' in migrate_data and migrate_data.vifs
            migrate_data.wait_for_vif_plugged = CONF.compute.live_migration_wait_for_vif_plug and using_multiple_port_bindings
            self.network_api.setup_networks_on_host(context, instance, self.host)
        except Exception:
            with excutils.save_and_reraise_exception():
                old_attachments = migrate_data.old_vol_attachment_ids
                for bdm in bdms:
                    if bdm.is_volume and bdm.attachment_id is not None and (bdm.volume_id in old_attachments):
                        self.volume_api.attachment_delete(context, bdm.attachment_id)
                        bdm.attachment_id = old_attachments[bdm.volume_id]
                        bdm.save()
        for bdm in bdms:
            if bdm.is_volume and bdm.attachment_id is not None:
                self.volume_api.attachment_complete(context, bdm.attachment_id)
        self._notify_about_instance_usage(context, instance, 'live_migration.pre.end', network_info=network_info)
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.LIVE_MIGRATION_PRE, phase=fields.NotificationPhase.END, bdms=bdms)
        LOG.debug('pre_live_migration result data is %s', migrate_data)
        return migrate_data

    @staticmethod
    def _neutron_failed_migration_callback(event_name, instance):
        msg = 'Neutron reported failure during migration with %(event)s for instance %(uuid)s'
        msg_args = {'event': event_name, 'uuid': instance.uuid}
        if CONF.vif_plugging_is_fatal:
            raise exception.VirtualInterfacePlugException(msg % msg_args)
        LOG.error(msg, msg_args)

    @staticmethod
    def _get_neutron_events_for_live_migration(instance):
        if CONF.vif_plugging_timeout:
            return instance.get_network_info().get_live_migration_plug_time_events()
        else:
            return []

    def _cleanup_pre_live_migration(self, context, dest, instance, migration, migrate_data, source_bdms):
        """Helper method for when pre_live_migration fails

        Sets the migration status to "error" and rolls back the live migration
        setup on the destination host.

        :param context: The user request context.
        :type context: nova.context.RequestContext
        :param dest: The live migration destination hostname.
        :type dest: str
        :param instance: The instance being live migrated.
        :type instance: nova.objects.Instance
        :param migration: The migration record tracking this live migration.
        :type migration: nova.objects.Migration
        :param migrate_data: Data about the live migration, populated from
                             the destination host.
        :type migrate_data: Subclass of nova.objects.LiveMigrateData
        :param source_bdms: BDMs prior to modification by the destination
                            compute host. Set by _do_live_migration and not
                            part of the callback interface, so this is never
                            None
        """
        self._set_migration_status(migration, 'error')
        migrate_data.migration = migration
        self._rollback_live_migration(context, instance, dest, migrate_data=migrate_data, source_bdms=source_bdms)

    def _do_pre_live_migration_from_source(self, context, dest, instance, block_migration, migration, migrate_data, source_bdms):
        """Prepares for pre-live-migration on the source host and calls dest

        Will setup a callback networking event handler (if configured) and
        then call the dest host's pre_live_migration method to prepare the
        dest host for live migration (plugs vifs, connect volumes, etc).

        _rollback_live_migration (on the source) will be called if
        pre_live_migration (on the dest) fails.

        :param context: nova auth request context for this operation
        :param dest: name of the destination compute service host
        :param instance: Instance object being live migrated
        :param block_migration: If true, prepare for block migration.
        :param migration: Migration object tracking this operation
        :param migrate_data: MigrateData object for this operation populated
            by the destination host compute driver as part of the
            check_can_live_migrate_destination call.
        :param source_bdms: BlockDeviceMappingList of BDMs currently attached
            to the instance from the source host.
        :returns: MigrateData object which is a modified version of the
            ``migrate_data`` argument from the compute driver on the dest
            host during the ``pre_live_migration`` call.
        :raises: MigrationError if waiting for the network-vif-plugged event
            timed out and is fatal.
        """

        class _BreakWaitForInstanceEvent(Exception):
            """Used as a signal to stop waiting for the network-vif-plugged
            event when we discover that
            [compute]/live_migration_wait_for_vif_plug is not set on the
            destination.
            """
            pass
        events = self._get_neutron_events_for_live_migration(instance)
        try:
            if 'block_migration' in migrate_data and migrate_data.block_migration:
                block_device_info = self._get_instance_block_device_info(context, instance, bdms=source_bdms)
                disk = self.driver.get_instance_disk_info(instance, block_device_info=block_device_info)
            else:
                disk = None
            deadline = CONF.vif_plugging_timeout
            error_cb = self._neutron_failed_migration_callback
            with self.virtapi.wait_for_instance_event(instance, events, deadline=deadline, error_callback=error_cb):
                with timeutils.StopWatch() as timer:
                    migrate_data = self.compute_rpcapi.pre_live_migration(context, instance, block_migration, disk, dest, migrate_data)
                LOG.info('Took %0.2f seconds for pre_live_migration on destination host %s.', timer.elapsed(), dest, instance=instance)
                wait_for_vif_plugged = 'wait_for_vif_plugged' in migrate_data and migrate_data.wait_for_vif_plugged
                if events and (not wait_for_vif_plugged):
                    raise _BreakWaitForInstanceEvent
        except _BreakWaitForInstanceEvent:
            if events:
                LOG.debug('Not waiting for events after pre_live_migration: %s. ', events, instance=instance)
        except exception.VirtualInterfacePlugException:
            with excutils.save_and_reraise_exception():
                LOG.exception('Failed waiting for network virtual interfaces to be plugged on the destination host %s.', dest, instance=instance)
                self._cleanup_pre_live_migration(context, dest, instance, migration, migrate_data, source_bdms)
        except eventlet.timeout.Timeout:
            msg = 'Timed out waiting for events: %(events)s. If these timeouts are a persistent issue it could mean the networking backend on host %(dest)s does not support sending these events unless there are port binding host changes which does not happen at this point in the live migration process. You may need to disable the live_migration_wait_for_vif_plug option on host %(dest)s.'
            subs = {'events': events, 'dest': dest}
            LOG.warning(msg, subs, instance=instance)
            if CONF.vif_plugging_is_fatal:
                self._cleanup_pre_live_migration(context, dest, instance, migration, migrate_data, source_bdms)
                raise exception.MigrationError(reason=msg % subs)
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.exception('Pre live migration failed at %s', dest, instance=instance)
                self._cleanup_pre_live_migration(context, dest, instance, migration, migrate_data, source_bdms)
        return migrate_data

    def _do_live_migration(self, context, dest, instance, block_migration, migration, migrate_data):
        self._set_migration_status(migration, 'preparing')
        source_bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(context, instance.uuid)
        migrate_data = self._do_pre_live_migration_from_source(context, dest, instance, block_migration, migration, migrate_data, source_bdms)
        migrate_data.migration = migration
        try:
            self._waiting_live_migrations.pop(instance.uuid)
        except KeyError:
            LOG.debug('Migration %s aborted by another process, rollback.', migration.uuid, instance=instance)
            self._rollback_live_migration(context, instance, dest, migrate_data, 'cancelled', source_bdms=source_bdms)
            self._notify_live_migrate_abort_end(context, instance)
            return
        self._set_migration_status(migration, 'running')
        post_live_migration = functools.partial(self._post_live_migration, source_bdms=source_bdms)
        rollback_live_migration = functools.partial(self._rollback_live_migration, source_bdms=source_bdms)
        LOG.debug('live_migration data is %s', migrate_data)
        try:
            self.driver.live_migration(context, instance, dest, post_live_migration, rollback_live_migration, block_migration, migrate_data)
        except Exception:
            LOG.exception('Live migration failed.', instance=instance)
            with excutils.save_and_reraise_exception():
                self._set_migration_status(migration, 'error')
                instance.refresh()
                self._set_instance_obj_error_state(instance, clean_task_state=True)

    @wrap_exception()
    @wrap_instance_event(prefix='compute')
    @errors_out_migration
    @wrap_instance_fault
    def live_migration(self, context, dest, instance, block_migration, migration, migrate_data):
        """Executing live migration.

        :param context: security context
        :param dest: destination host
        :param instance: a nova.objects.instance.Instance object
        :param block_migration: if true, prepare for block migration
        :param migration: an nova.objects.Migration object
        :param migrate_data: implementation specific params

        """
        self._set_migration_status(migration, 'queued')
        self._waiting_live_migrations[instance.uuid] = (None, None)
        try:
            future = self._live_migration_executor.submit(self._do_live_migration, context, dest, instance, block_migration, migration, migrate_data)
            self._waiting_live_migrations[instance.uuid] = (migration, future)
        except RuntimeError:
            LOG.info('Migration %s failed to submit as the compute service is shutting down.', migration.uuid, instance=instance)
            raise exception.LiveMigrationNotSubmitted(migration_uuid=migration.uuid, instance_uuid=instance.uuid)

    @wrap_exception()
    @wrap_instance_event(prefix='compute')
    @wrap_instance_fault
    def live_migration_force_complete(self, context, instance):
        """Force live migration to complete.

        :param context: Security context
        :param instance: The instance that is being migrated
        """
        self._notify_about_instance_usage(context, instance, 'live.migration.force.complete.start')
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.LIVE_MIGRATION_FORCE_COMPLETE, phase=fields.NotificationPhase.START)
        self.driver.live_migration_force_complete(instance)
        self._notify_about_instance_usage(context, instance, 'live.migration.force.complete.end')
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.LIVE_MIGRATION_FORCE_COMPLETE, phase=fields.NotificationPhase.END)

    def _notify_live_migrate_abort_end(self, context, instance):
        self._notify_about_instance_usage(context, instance, 'live.migration.abort.end')
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.LIVE_MIGRATION_ABORT, phase=fields.NotificationPhase.END)

    @wrap_exception()
    @wrap_instance_event(prefix='compute')
    @wrap_instance_fault
    def live_migration_abort(self, context, instance, migration_id):
        """Abort an in-progress live migration.

        :param context: Security context
        :param instance: The instance that is being migrated
        :param migration_id: ID of in-progress live migration

        """
        self._notify_about_instance_usage(context, instance, 'live.migration.abort.start')
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.LIVE_MIGRATION_ABORT, phase=fields.NotificationPhase.START)
        try:
            (migration, future) = self._waiting_live_migrations.pop(instance.uuid)
            if future and future.cancel():
                self._set_migration_status(migration, 'cancelled')
        except KeyError:
            migration = objects.Migration.get_by_id(context, migration_id)
            if migration.status != 'running':
                raise exception.InvalidMigrationState(migration_id=migration_id, instance_uuid=instance.uuid, state=migration.status, method='abort live migration')
            self.driver.live_migration_abort(instance)
        self._notify_live_migrate_abort_end(context, instance)

    def _live_migration_cleanup_flags(self, migrate_data, migr_ctxt=None):
        """Determine whether disks, instance path or other resources
        need to be cleaned up after live migration (at source on success,
        at destination on rollback)

        Block migration needs empty image at destination host before migration
        starts, so if any failure occurs, any empty images has to be deleted.

        Also Volume backed live migration w/o shared storage needs to delete
        newly created instance-xxx dir on the destination as a part of its
        rollback process

        There may be other resources which need cleanup; currently this is
        limited to vPMEM devices with the libvirt driver.

        :param migrate_data: implementation specific data
        :param migr_ctxt: specific resources stored in migration_context
        :returns: (bool, bool) -- do_cleanup, destroy_disks
        """
        do_cleanup = False
        destroy_disks = False
        if isinstance(migrate_data, migrate_data_obj.LibvirtLiveMigrateData):
            has_vpmem = False
            if migr_ctxt and migr_ctxt.old_resources:
                for resource in migr_ctxt.old_resources:
                    if 'metadata' in resource and isinstance(resource.metadata, objects.LibvirtVPMEMDevice):
                        has_vpmem = True
                        break
            do_cleanup = not migrate_data.is_shared_instance_path or has_vpmem
            destroy_disks = not migrate_data.is_shared_block_storage
        elif isinstance(migrate_data, migrate_data_obj.HyperVLiveMigrateData):
            do_cleanup = True
            destroy_disks = not migrate_data.is_shared_instance_path
        return (do_cleanup, destroy_disks)

    def _post_live_migration_remove_source_vol_connections(self, context, instance, source_bdms):
        """Disconnect volume connections from the source host during
        _post_live_migration.

        :param context: nova auth RequestContext
        :param instance: Instance object being live migrated
        :param source_bdms: BlockDeviceMappingList representing the attached
            volumes with connection_info set for the source host
        """
        connector = self.driver.get_volume_connector(instance)
        for bdm in source_bdms:
            if bdm.is_volume:
                try:
                    if bdm.attachment_id is None:
                        self.volume_api.terminate_connection(context, bdm.volume_id, connector)
                    else:
                        self.volume_api.attachment_delete(context, bdm.attachment_id)
                except Exception as e:
                    if bdm.attachment_id is None:
                        LOG.error('Connection for volume %s not terminated on source host %s during post_live_migration: %s', bdm.volume_id, self.host, str(e), instance=instance)
                    else:
                        LOG.error('Volume attachment %s not deleted on source host %s during post_live_migration: %s', bdm.attachment_id, self.host, str(e), instance=instance)

    @wrap_exception()
    @wrap_instance_fault
    def _post_live_migration(self, ctxt, instance, dest, block_migration=False, migrate_data=None, source_bdms=None):
        """Post operations for live migration.

        This method is called from live_migration
        and mainly updating database record.

        :param ctxt: security context
        :param instance: instance dict
        :param dest: destination host
        :param block_migration: if true, prepare for block migration
        :param migrate_data: if not None, it is a dict which has data
        :param source_bdms: BDMs prior to modification by the destination
                            compute host. Set by _do_live_migration and not
                            part of the callback interface, so this is never
                            None
        required for live migration without shared storage

        """
        LOG.info('_post_live_migration() is started..', instance=instance)
        block_device_info = self._get_instance_block_device_info(ctxt, instance, bdms=source_bdms)
        self.driver.post_live_migration(ctxt, instance, block_device_info, migrate_data)
        self._post_live_migration_remove_source_vol_connections(ctxt, instance, source_bdms)
        network_info = instance.get_network_info()
        self._notify_about_instance_usage(ctxt, instance, 'live_migration._post.start', network_info=network_info)
        compute_utils.notify_about_instance_action(ctxt, instance, self.host, action=fields.NotificationAction.LIVE_MIGRATION_POST, phase=fields.NotificationPhase.START)
        migration = {'source_compute': self.host, 'dest_compute': dest}
        self.network_api.migrate_instance_start(ctxt, instance, migration)
        destroy_vifs = False
        try:
            unplug_nw_info = network_info
            if migrate_data and 'vifs' in migrate_data:
                nw_info = []
                for migrate_vif in migrate_data.vifs:
                    nw_info.append(migrate_vif.source_vif)
                unplug_nw_info = network_model.NetworkInfo.hydrate(nw_info)
                LOG.debug('Calling driver.post_live_migration_at_source with original source VIFs from migrate_data: %s', unplug_nw_info, instance=instance)
            self.driver.post_live_migration_at_source(ctxt, instance, unplug_nw_info)
        except NotImplementedError as ex:
            LOG.debug(ex, instance=instance)
            destroy_vifs = True
        self.rt.free_pci_device_allocations_for_instance(ctxt, instance)
        source_node = instance.node
        (do_cleanup, destroy_disks) = self._live_migration_cleanup_flags(migrate_data, migr_ctxt=instance.migration_context)
        if do_cleanup:
            LOG.debug('Calling driver.cleanup from _post_live_migration', instance=instance)
            self.driver.cleanup(ctxt, instance, unplug_nw_info, destroy_disks=destroy_disks, migrate_data=migrate_data, destroy_vifs=destroy_vifs)
        post_at_dest_success = True
        try:
            self.compute_rpcapi.post_live_migration_at_destination(ctxt, instance, block_migration, dest)
        except Exception as error:
            post_at_dest_success = False
            LOG.exception('Post live migration at destination %s failed', dest, instance=instance, error=error)
        self.instance_events.clear_events_for_instance(instance)
        self.update_available_resource(ctxt)
        self._update_scheduler_instance_info(ctxt, instance)
        self._notify_about_instance_usage(ctxt, instance, 'live_migration._post.end', network_info=network_info)
        compute_utils.notify_about_instance_action(ctxt, instance, self.host, action=fields.NotificationAction.LIVE_MIGRATION_POST, phase=fields.NotificationPhase.END)
        if post_at_dest_success:
            LOG.info('Migrating instance to %s finished successfully.', dest, instance=instance)
        self._clean_instance_console_tokens(ctxt, instance)
        if migrate_data and migrate_data.obj_attr_is_set('migration'):
            migrate_data.migration.status = 'completed'
            migrate_data.migration.save()
            self._delete_allocation_after_move(ctxt, instance, migrate_data.migration)
        else:
            LOG.warning('Live migration ended with no migrate_data record. Unable to clean up migration-based allocations for node %s which is almost certainly not an expected situation.', source_node, instance=instance)

    def _consoles_enabled(self):
        """Returns whether a console is enable."""
        return CONF.vnc.enabled or CONF.spice.enabled or CONF.rdp.enabled or CONF.serial_console.enabled or CONF.mks.enabled

    def _clean_instance_console_tokens(self, ctxt, instance):
        """Clean console tokens stored for an instance."""
        if self._consoles_enabled():
            objects.ConsoleAuthToken.clean_console_auths_for_instance(ctxt, instance.uuid)

    @wrap_exception()
    @wrap_instance_event(prefix='compute')
    @wrap_instance_fault
    def post_live_migration_at_destination(self, context, instance, block_migration):
        """Post operations for live migration .

        :param context: security context
        :param instance: Instance dict
        :param block_migration: if true, prepare for block migration

        """
        LOG.info('Post operation of migration started', instance=instance)
        self.network_api.setup_networks_on_host(context, instance, self.host)
        migration = objects.Migration(source_compute=instance.host, dest_compute=self.host, migration_type=fields.MigrationType.LIVE_MIGRATION)
        self.network_api.migrate_instance_finish(context, instance, migration, provider_mappings=None)
        network_info = self.network_api.get_instance_nw_info(context, instance)
        self._notify_about_instance_usage(context, instance, 'live_migration.post.dest.start', network_info=network_info)
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.LIVE_MIGRATION_POST_DEST, phase=fields.NotificationPhase.START)
        block_device_info = self._get_instance_block_device_info(context, instance)
        self.rt.allocate_pci_devices_for_instance(context, instance)
        try:
            self.driver.post_live_migration_at_destination(context, instance, network_info, block_migration, block_device_info)
        except Exception:
            with excutils.save_and_reraise_exception():
                instance.vm_state = vm_states.ERROR
                LOG.error('Unexpected error during post live migration at destination host.', instance=instance)
        finally:
            current_power_state = self._get_power_state(instance)
            node_name = None
            prev_host = instance.host
            try:
                compute_node = self._get_compute_info(context, self.host)
                node_name = compute_node.hypervisor_hostname
            except exception.ComputeHostNotFound:
                LOG.exception('Failed to get compute_info for %s', self.host)
            finally:
                instance.apply_migration_context()
                instance.drop_migration_context()
                instance.host = self.host
                instance.power_state = current_power_state
                instance.task_state = None
                instance.node = node_name
                instance.progress = 0
                instance.save(expected_task_state=task_states.MIGRATING)
        try:
            self.network_api.setup_networks_on_host(context, instance, prev_host, teardown=True)
        except exception.PortBindingDeletionFailed as e:
            LOG.error('Network cleanup failed for source host %s during post live migration. You may need to manually clean up resources in the network service. Error: %s', prev_host, str(e))
        self.network_api.setup_networks_on_host(context, instance, self.host)
        self._notify_about_instance_usage(context, instance, 'live_migration.post.dest.end', network_info=network_info)
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.LIVE_MIGRATION_POST_DEST, phase=fields.NotificationPhase.END)

    def _remove_remote_volume_connections(self, context, dest, bdms, instance):
        """Rollback remote volume connections on the dest"""
        for bdm in bdms:
            try:
                self.compute_rpcapi.remove_volume_connection(context, instance, bdm.volume_id, dest)
            except Exception:
                LOG.warning('Ignoring exception while attempting to rollback volume connections for volume %s on host %s.', bdm.volume_id, dest, instance=instance)

    def _rollback_volume_bdms(self, context, bdms, original_bdms, instance):
        """Rollback the connection_info and attachment_id for each bdm"""
        original_bdms_by_volid = {bdm.volume_id: bdm for bdm in original_bdms if bdm.is_volume}
        for bdm in bdms:
            try:
                original_bdm = original_bdms_by_volid[bdm.volume_id]
                if bdm.attachment_id and original_bdm.attachment_id and (bdm.attachment_id != original_bdm.attachment_id):
                    self.volume_api.attachment_delete(context, bdm.attachment_id)
                    bdm.attachment_id = original_bdm.attachment_id
                bdm.connection_info = original_bdm.connection_info
                bdm.save()
            except cinder_exception.ClientException:
                LOG.warning('Ignoring cinderclient exception when attempting to delete attachment %s for volume %s while rolling back volume bdms.', bdm.attachment_id, bdm.volume_id, instance=instance)
            except Exception:
                with excutils.save_and_reraise_exception():
                    LOG.exception('Exception while attempting to rollback BDM for volume %s.', bdm.volume_id, instance=instance)

    @wrap_exception()
    @wrap_instance_fault
    def _rollback_live_migration(self, context, instance, dest, migrate_data=None, migration_status='failed', source_bdms=None):
        """Recovers Instance/volume state from migrating -> running.

        :param context: security context
        :param instance: nova.objects.instance.Instance object
        :param dest:
            This method is called from live migration src host.
            This param specifies destination host.
        :param migrate_data:
            if not none, contains implementation specific data.
        :param migration_status:
            Contains the status we want to set for the migration object
        :param source_bdms: BDMs prior to modification by the destination
                            compute host. Set by _do_live_migration and not
                            part of the callback interface, so this is never
                            None

        """
        instance.pci_requests = objects.InstancePCIRequests.get_by_instance_uuid(context, instance.uuid)
        if isinstance(migrate_data, migrate_data_obj.LiveMigrateData) and migrate_data.obj_attr_is_set('migration'):
            migration = migrate_data.migration
        else:
            migration = None
        if migration:
            self._revert_allocation(context, instance, migration)
        else:
            LOG.error('Unable to revert allocations during live migration rollback; compute driver did not provide migrate_data', instance=instance)
        self.network_api.setup_networks_on_host(context, instance, self.host)
        self.driver.rollback_live_migration_at_source(context, instance, migrate_data)
        bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(context, instance.uuid)
        vol_bdms = [bdm for bdm in bdms if bdm.is_volume]
        self._remove_remote_volume_connections(context, dest, vol_bdms, instance)
        self._rollback_volume_bdms(context, vol_bdms, source_bdms, instance)
        self._notify_about_instance_usage(context, instance, 'live_migration._rollback.start')
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.LIVE_MIGRATION_ROLLBACK, phase=fields.NotificationPhase.START, bdms=bdms)
        (do_cleanup, destroy_disks) = self._live_migration_cleanup_flags(migrate_data, migr_ctxt=instance.migration_context)
        if do_cleanup:
            self.compute_rpcapi.rollback_live_migration_at_destination(context, instance, dest, destroy_disks=destroy_disks, migrate_data=migrate_data)
        else:
            with errors_out_migration_ctxt(migration):
                try:
                    self.network_api.setup_networks_on_host(context, instance, host=dest, teardown=True)
                except exception.PortBindingDeletionFailed as e:
                    LOG.error('Network cleanup failed for destination host %s during live migration rollback. You may need to manually clean up resources in the network service. Error: %s', dest, str(e))
                except Exception:
                    with excutils.save_and_reraise_exception():
                        LOG.exception('An error occurred while cleaning up networking during live migration rollback.', instance=instance)
        if 'src_supports_numa_live_migration' in migrate_data and migrate_data.src_supports_numa_live_migration:
            LOG.debug('Calling destination to drop move claim.', instance=instance)
            self.compute_rpcapi.drop_move_claim_at_destination(context, instance, dest)
        instance.task_state = None
        instance.progress = 0
        instance.drop_migration_context()
        instance.save(expected_task_state=[task_states.MIGRATING])
        self._notify_about_instance_usage(context, instance, 'live_migration._rollback.end')
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.LIVE_MIGRATION_ROLLBACK, phase=fields.NotificationPhase.END, bdms=bdms)
        self._set_migration_status(migration, migration_status)

    @wrap_exception()
    @wrap_instance_fault
    def drop_move_claim_at_destination(self, context, instance):
        """Called by the source of a live migration during rollback to ask the
        destination to drop the MoveClaim object that was created for the live
        migration on the destination.
        """
        nodename = self._get_nodename(instance)
        LOG.debug('Dropping live migration resource claim on destination node %s', nodename, instance=instance)
        self.rt.drop_move_claim(context, instance, nodename, instance_type=instance.flavor)

    @wrap_exception()
    @wrap_instance_event(prefix='compute')
    @wrap_instance_fault
    def rollback_live_migration_at_destination(self, context, instance, destroy_disks, migrate_data):
        """Cleaning up image directory that is created pre_live_migration.

        :param context: security context
        :param instance: a nova.objects.instance.Instance object sent over rpc
        :param destroy_disks: whether to destroy volumes or not
        :param migrate_data: contains migration info
        """
        network_info = self.network_api.get_instance_nw_info(context, instance)
        self._notify_about_instance_usage(context, instance, 'live_migration.rollback.dest.start', network_info=network_info)
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.LIVE_MIGRATION_ROLLBACK_DEST, phase=fields.NotificationPhase.START)
        try:
            self.network_api.setup_networks_on_host(context, instance, self.host, teardown=True)
        except exception.PortBindingDeletionFailed as e:
            LOG.error('Network cleanup failed for destination host %s during live migration rollback. You may need to manually clean up resources in the network service. Error: %s', self.host, str(e))
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.exception('An error occurred while deallocating network.', instance=instance)
        finally:
            block_device_info = self._get_instance_block_device_info(context, instance)
            self.rt.free_pci_device_claims_for_instance(context, instance)
            with instance.mutated_migration_context():
                self.driver.rollback_live_migration_at_destination(context, instance, network_info, block_device_info, destroy_disks=destroy_disks, migrate_data=migrate_data)
        self._notify_about_instance_usage(context, instance, 'live_migration.rollback.dest.end', network_info=network_info)
        compute_utils.notify_about_instance_action(context, instance, self.host, action=fields.NotificationAction.LIVE_MIGRATION_ROLLBACK_DEST, phase=fields.NotificationPhase.END)

    def _require_nw_info_update(self, context, instance):
        """Detect whether there is a mismatch in binding:host_id, or
        binding_failed or unbound binding:vif_type for any of the instances
        ports.
        """
        if self.driver.manages_network_binding_host_id():
            return False
        search_opts = {'device_id': instance.uuid, 'fields': ['binding:host_id', 'binding:vif_type']}
        ports = self.network_api.list_ports(context, **search_opts)
        for p in ports['ports']:
            if p.get('binding:host_id') != self.host:
                return True
            vif_type = p.get('binding:vif_type')
            if vif_type == network_model.VIF_TYPE_UNBOUND or vif_type == network_model.VIF_TYPE_BINDING_FAILED:
                return True
        return False

    @periodic_task.periodic_task(spacing=CONF.heal_instance_info_cache_interval)
    def _heal_instance_info_cache(self, context):
        """Called periodically.  On every call, try to update the
        info_cache's network information for another instance by
        calling to the network manager.

        This is implemented by keeping a cache of uuids of instances
        that live on this host.  On each call, we pop one off of a
        list, pull the DB record, and try the call to the network API.
        If anything errors don't fail, as it's possible the instance
        has been deleted, etc.
        """
        heal_interval = CONF.heal_instance_info_cache_interval
        if not heal_interval:
            return
        instance_uuids = getattr(self, '_instance_uuids_to_heal', [])
        instance = None
        LOG.debug('Starting heal instance info cache')
        if not instance_uuids:
            LOG.debug('Rebuilding the list of instances to heal')
            db_instances = objects.InstanceList.get_by_host(context, self.host, expected_attrs=[], use_slave=True)
            for inst in db_instances:
                if inst.vm_state == vm_states.BUILDING:
                    LOG.debug('Skipping network cache update for instance because it is Building.', instance=inst)
                    continue
                if inst.task_state == task_states.DELETING:
                    LOG.debug('Skipping network cache update for instance because it is being deleted.', instance=inst)
                    continue
                if not instance:
                    instance = inst
                else:
                    instance_uuids.append(inst['uuid'])
            self._instance_uuids_to_heal = instance_uuids
        else:
            while instance_uuids:
                try:
                    inst = objects.Instance.get_by_uuid(context, instance_uuids.pop(0), expected_attrs=['system_metadata', 'info_cache', 'flavor'], use_slave=True)
                except exception.InstanceNotFound:
                    continue
                if inst.host != self.host:
                    LOG.debug('Skipping network cache update for instance because it has been migrated to another host.', instance=inst)
                elif inst.task_state == task_states.DELETING:
                    LOG.debug('Skipping network cache update for instance because it is being deleted.', instance=inst)
                else:
                    instance = inst
                    break
        if instance:
            try:
                if instance.task_state is None and self._require_nw_info_update(context, instance):
                    LOG.info('Updating ports in neutron', instance=instance)
                    self.network_api.setup_instance_network_on_host(context, instance, self.host)
                self.network_api.get_instance_nw_info(context, instance, force_refresh=True)
                LOG.debug('Updated the network info_cache for instance', instance=instance)
            except exception.InstanceNotFound:
                LOG.debug('Instance no longer exists. Unable to refresh', instance=instance)
                return
            except exception.InstanceInfoCacheNotFound:
                LOG.debug('InstanceInfoCache no longer exists. Unable to refresh', instance=instance)
            except Exception:
                LOG.error('An error occurred while refreshing the network cache.', instance=instance, exc_info=True)
        else:
            LOG.debug("Didn't find any instances for network info cache update.")

    @periodic_task.periodic_task
    def _poll_rebooting_instances(self, context):
        if CONF.reboot_timeout > 0:
            filters = {'task_state': [task_states.REBOOTING, task_states.REBOOT_STARTED, task_states.REBOOT_PENDING], 'host': self.host}
            rebooting = objects.InstanceList.get_by_filters(context, filters, expected_attrs=[], use_slave=True)
            to_poll = []
            for instance in rebooting:
                if timeutils.is_older_than(instance.updated_at, CONF.reboot_timeout):
                    to_poll.append(instance)
            self.driver.poll_rebooting_instances(CONF.reboot_timeout, to_poll)

    @periodic_task.periodic_task
    def _poll_rescued_instances(self, context):
        if CONF.rescue_timeout > 0:
            filters = {'vm_state': vm_states.RESCUED, 'host': self.host}
            rescued_instances = objects.InstanceList.get_by_filters(context, filters, expected_attrs=['system_metadata'], use_slave=True)
            to_unrescue = []
            for instance in rescued_instances:
                if timeutils.is_older_than(instance.launched_at, CONF.rescue_timeout):
                    to_unrescue.append(instance)
            for instance in to_unrescue:
                self.compute_api.unrescue(context, instance)

    @periodic_task.periodic_task
    def _poll_unconfirmed_resizes(self, context):
        if CONF.resize_confirm_window == 0:
            return
        migrations = objects.MigrationList.get_unconfirmed_by_dest_compute(context, CONF.resize_confirm_window, self.host, use_slave=True)
        migrations_info = dict(migration_count=len(migrations), confirm_window=CONF.resize_confirm_window)
        if migrations_info['migration_count'] > 0:
            LOG.info('Found %(migration_count)d unconfirmed migrations older than %(confirm_window)d seconds', migrations_info)

        def _set_migration_to_error(migration, reason, **kwargs):
            LOG.warning('Setting migration %(migration_id)s to error: %(reason)s', {'migration_id': migration['id'], 'reason': reason}, **kwargs)
            migration.status = 'error'
            migration.save()
        for migration in migrations:
            instance_uuid = migration.instance_uuid
            LOG.info('Automatically confirming migration %(migration_id)s for instance %(instance_uuid)s', {'migration_id': migration.id, 'instance_uuid': instance_uuid})
            expected_attrs = ['metadata', 'system_metadata']
            try:
                instance = objects.Instance.get_by_uuid(context, instance_uuid, expected_attrs=expected_attrs, use_slave=True)
            except exception.InstanceNotFound:
                reason = _('Instance %s not found') % instance_uuid
                _set_migration_to_error(migration, reason)
                continue
            if instance.vm_state == vm_states.ERROR:
                reason = _('In ERROR state')
                _set_migration_to_error(migration, reason, instance=instance)
                continue
            if instance.task_state in [task_states.DELETING, task_states.SOFT_DELETING]:
                msg = 'Instance being deleted or soft deleted during resize confirmation. Skipping.'
                LOG.debug(msg, instance=instance)
                continue
            if instance.task_state == task_states.RESIZE_FINISH:
                msg = 'Instance still resizing during resize confirmation. Skipping.'
                LOG.debug(msg, instance=instance)
                continue
            vm_state = instance.vm_state
            task_state = instance.task_state
            if vm_state != vm_states.RESIZED or task_state is not None:
                reason = _('In states %(vm_state)s/%(task_state)s, not RESIZED/None') % {'vm_state': vm_state, 'task_state': task_state}
                _set_migration_to_error(migration, reason, instance=instance)
                continue
            try:
                self.compute_api.confirm_resize(context, instance, migration=migration)
            except Exception as e:
                LOG.info('Error auto-confirming resize: %s. Will retry later.', e, instance=instance)

    @periodic_task.periodic_task(spacing=CONF.shelved_poll_interval)
    def _poll_shelved_instances(self, context):
        if CONF.shelved_offload_time <= 0:
            return
        filters = {'vm_state': vm_states.SHELVED, 'task_state': None, 'host': self.host}
        shelved_instances = objects.InstanceList.get_by_filters(context, filters=filters, expected_attrs=['system_metadata'], use_slave=True)
        to_gc = []
        for instance in shelved_instances:
            sys_meta = instance.system_metadata
            shelved_at = timeutils.parse_strtime(sys_meta['shelved_at'])
            if timeutils.is_older_than(shelved_at, CONF.shelved_offload_time):
                to_gc.append(instance)
        cyclient = cyborg.get_client(context)
        for instance in to_gc:
            try:
                instance.task_state = task_states.SHELVING_OFFLOADING
                instance.save(expected_task_state=(None,))
                accel_uuids = []
                if instance.flavor.extra_specs.get('accel:device_profile'):
                    accel_uuids = cyclient.get_arq_uuids_for_instance(instance)
                self.shelve_offload_instance(context, instance, clean_shutdown=False, accel_uuids=accel_uuids)
            except Exception:
                LOG.exception('Periodic task failed to offload instance.', instance=instance)

    @periodic_task.periodic_task
    def _instance_usage_audit(self, context):
        if not CONF.instance_usage_audit:
            return
        (begin, end) = utils.last_completed_audit_period()
        if objects.TaskLog.get(context, 'instance_usage_audit', begin, end, self.host):
            return
        instances = objects.InstanceList.get_active_by_window_joined(context, begin, end, host=self.host, expected_attrs=['system_metadata', 'info_cache', 'metadata', 'flavor'], use_slave=True)
        num_instances = len(instances)
        errors = 0
        successes = 0
        LOG.info('Running instance usage audit for host %(host)s from %(begin_time)s to %(end_time)s. %(number_instances)s instances.', {'host': self.host, 'begin_time': begin, 'end_time': end, 'number_instances': num_instances})
        start_time = time.time()
        task_log = objects.TaskLog(context)
        task_log.task_name = 'instance_usage_audit'
        task_log.period_beginning = begin
        task_log.period_ending = end
        task_log.host = self.host
        task_log.task_items = num_instances
        task_log.message = 'Instance usage audit started...'
        task_log.begin_task()
        for instance in instances:
            try:
                compute_utils.notify_usage_exists(self.notifier, context, instance, self.host, ignore_missing_network_data=False)
                successes += 1
            except Exception:
                LOG.exception('Failed to generate usage audit for instance on host %s', self.host, instance=instance)
                errors += 1
        task_log.errors = errors
        task_log.message = 'Instance usage audit ran for host %s, %s instances in %s seconds.' % (self.host, num_instances, time.time() - start_time)
        task_log.end_task()

    def _get_host_volume_bdms(self, context, use_slave=False):
        """Return all block device mappings on a compute host."""
        compute_host_bdms = []
        instances = objects.InstanceList.get_by_host(context, self.host, use_slave=use_slave)
        for instance in instances:
            bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(context, instance.uuid, use_slave=use_slave)
            instance_bdms = [bdm for bdm in bdms if bdm.is_volume]
            compute_host_bdms.append(dict(instance=instance, instance_bdms=instance_bdms))
        return compute_host_bdms

    def _update_volume_usage_cache(self, context, vol_usages):
        """Updates the volume usage cache table with a list of stats."""
        for usage in vol_usages:
            greenthread.sleep(0)
            vol_usage = objects.VolumeUsage(context)
            vol_usage.volume_id = usage['volume']
            vol_usage.instance_uuid = usage['instance'].uuid
            vol_usage.project_id = usage['instance'].project_id
            vol_usage.user_id = usage['instance'].user_id
            vol_usage.availability_zone = usage['instance'].availability_zone
            vol_usage.curr_reads = usage['rd_req']
            vol_usage.curr_read_bytes = usage['rd_bytes']
            vol_usage.curr_writes = usage['wr_req']
            vol_usage.curr_write_bytes = usage['wr_bytes']
            vol_usage.save()
            self.notifier.info(context, 'volume.usage', vol_usage.to_dict())
            compute_utils.notify_about_volume_usage(context, vol_usage, self.host)

    @periodic_task.periodic_task(spacing=CONF.volume_usage_poll_interval)
    def _poll_volume_usage(self, context):
        if CONF.volume_usage_poll_interval == 0:
            return
        compute_host_bdms = self._get_host_volume_bdms(context, use_slave=True)
        if not compute_host_bdms:
            return
        LOG.debug('Updating volume usage cache')
        try:
            vol_usages = self.driver.get_all_volume_usage(context, compute_host_bdms)
        except NotImplementedError:
            return
        self._update_volume_usage_cache(context, vol_usages)

    @periodic_task.periodic_task(spacing=CONF.sync_power_state_interval, run_immediately=True)
    def _sync_power_states(self, context):
        """Align power states between the database and the hypervisor.

        To sync power state data we make a DB call to get the number of
        virtual machines known by the hypervisor and if the number matches the
        number of virtual machines known by the database, we proceed in a lazy
        loop, one database record at a time, checking if the hypervisor has the
        same power state as is in the database.
        """
        db_instances = objects.InstanceList.get_by_host(context, self.host, expected_attrs=[], use_slave=True)
        try:
            num_vm_instances = self.driver.get_num_instances()
        except exception.VirtDriverNotReady as e:
            LOG.info('Skipping _sync_power_states periodic task due to: %s', e)
            return
        num_db_instances = len(db_instances)
        if num_vm_instances != num_db_instances:
            LOG.warning('While synchronizing instance power states, found %(num_db_instances)s instances in the database and %(num_vm_instances)s instances on the hypervisor.', {'num_db_instances': num_db_instances, 'num_vm_instances': num_vm_instances})

        def _sync(db_instance):

            @utils.synchronized(db_instance.uuid)
            def query_driver_power_state_and_sync():
                self._query_driver_power_state_and_sync(context, db_instance)
            try:
                query_driver_power_state_and_sync()
            except Exception:
                LOG.exception('Periodic sync_power_state task had an error while processing an instance.', instance=db_instance)
            self._syncs_in_progress.pop(db_instance.uuid)
        for db_instance in db_instances:
            uuid = db_instance.uuid
            if uuid in self._syncs_in_progress:
                LOG.debug('Sync already in progress for %s', uuid)
            else:
                LOG.debug('Triggering sync for uuid %s', uuid)
                self._syncs_in_progress[uuid] = True
                self._sync_power_pool.spawn_n(_sync, db_instance)

    def _query_driver_power_state_and_sync(self, context, db_instance):
        if db_instance.task_state is not None:
            LOG.info('During sync_power_state the instance has a pending task (%(task)s). Skip.', {'task': db_instance.task_state}, instance=db_instance)
            return
        try:
            vm_instance = self.driver.get_info(db_instance)
            vm_power_state = vm_instance.state
        except exception.InstanceNotFound:
            vm_power_state = power_state.NOSTATE
        try:
            self._sync_instance_power_state(context, db_instance, vm_power_state, use_slave=True)
        except exception.InstanceNotFound:
            pass

    def _stop_unexpected_shutdown_instance(self, context, vm_state, db_instance, orig_db_power_state):
        vm_instance = self.driver.get_info(db_instance, use_cache=False)
        vm_power_state = vm_instance.state
        if vm_power_state in (power_state.SHUTDOWN, power_state.CRASHED):
            LOG.warning('Instance shutdown by itself. Calling the stop API. Current vm_state: %(vm_state)s, current task_state: %(task_state)s, original DB power_state: %(db_power_state)s, current VM power_state: %(vm_power_state)s', {'vm_state': vm_state, 'task_state': db_instance.task_state, 'db_power_state': orig_db_power_state, 'vm_power_state': vm_power_state}, instance=db_instance)
            try:
                if db_instance.shutdown_terminate:
                    self.compute_api.delete(context, db_instance)
                else:
                    self.compute_api.stop(context, db_instance)
            except Exception:
                LOG.exception('error during stop() in sync_power_state.', instance=db_instance)

    def _sync_instance_power_state(self, context, db_instance, vm_power_state, use_slave=False):
        """Align instance power state between the database and hypervisor.

        If the instance is not found on the hypervisor, but is in the database,
        then a stop() API will be called on the instance.
        """
        db_instance.refresh(use_slave=use_slave)
        db_power_state = db_instance.power_state
        vm_state = db_instance.vm_state
        if self.host != db_instance.host:
            LOG.info('During the sync_power process the instance has moved from host %(src)s to host %(dst)s', {'src': db_instance.host, 'dst': self.host}, instance=db_instance)
            return
        elif db_instance.task_state is not None:
            LOG.info('During sync_power_state the instance has a pending task (%(task)s). Skip.', {'task': db_instance.task_state}, instance=db_instance)
            return
        orig_db_power_state = db_power_state
        if vm_power_state != db_power_state:
            LOG.info('During _sync_instance_power_state the DB power_state (%(db_power_state)s) does not match the vm_power_state from the hypervisor (%(vm_power_state)s). Updating power_state in the DB to match the hypervisor.', {'db_power_state': db_power_state, 'vm_power_state': vm_power_state}, instance=db_instance)
            db_instance.power_state = vm_power_state
            db_instance.save()
            db_power_state = vm_power_state
        if vm_state in (vm_states.BUILDING, vm_states.RESCUED, vm_states.RESIZED, vm_states.SUSPENDED, vm_states.ERROR):
            pass
        elif vm_state == vm_states.ACTIVE:
            if vm_power_state in (power_state.SHUTDOWN, power_state.CRASHED):
                self._stop_unexpected_shutdown_instance(context, vm_state, db_instance, orig_db_power_state)
            elif vm_power_state == power_state.SUSPENDED:
                LOG.warning('Instance is suspended unexpectedly. Calling the stop API.', instance=db_instance)
                try:
                    self.compute_api.stop(context, db_instance)
                except Exception:
                    LOG.exception('error during stop() in sync_power_state.', instance=db_instance)
            elif vm_power_state == power_state.PAUSED:
                LOG.warning('Instance is paused unexpectedly. Ignore.', instance=db_instance)
            elif vm_power_state == power_state.NOSTATE:
                LOG.warning('Instance is unexpectedly not found. Ignore.', instance=db_instance)
        elif vm_state == vm_states.STOPPED:
            if vm_power_state not in (power_state.NOSTATE, power_state.SHUTDOWN, power_state.CRASHED):
                LOG.warning('Instance is not stopped. Calling the stop API. Current vm_state: %(vm_state)s, current task_state: %(task_state)s, original DB power_state: %(db_power_state)s, current VM power_state: %(vm_power_state)s', {'vm_state': vm_state, 'task_state': db_instance.task_state, 'db_power_state': orig_db_power_state, 'vm_power_state': vm_power_state}, instance=db_instance)
                try:
                    self.compute_api.force_stop(context, db_instance)
                except Exception:
                    LOG.exception('error during stop() in sync_power_state.', instance=db_instance)
        elif vm_state == vm_states.PAUSED:
            if vm_power_state in (power_state.SHUTDOWN, power_state.CRASHED):
                LOG.warning('Paused instance shutdown by itself. Calling the stop API.', instance=db_instance)
                try:
                    self.compute_api.force_stop(context, db_instance)
                except Exception:
                    LOG.exception('error during stop() in sync_power_state.', instance=db_instance)
        elif vm_state in (vm_states.SOFT_DELETED, vm_states.DELETED):
            if vm_power_state not in (power_state.NOSTATE, power_state.SHUTDOWN):
                LOG.warning('Instance is not (soft-)deleted.', instance=db_instance)

    @periodic_task.periodic_task
    def _reclaim_queued_deletes(self, context):
        """Reclaim instances that are queued for deletion."""
        interval = CONF.reclaim_instance_interval
        if interval <= 0:
            LOG.debug('CONF.reclaim_instance_interval <= 0, skipping...')
            return
        filters = {'vm_state': vm_states.SOFT_DELETED, 'task_state': None, 'host': self.host}
        instances = objects.InstanceList.get_by_filters(context, filters, expected_attrs=objects.instance.INSTANCE_DEFAULT_FIELDS, use_slave=True)
        for instance in instances:
            if self._deleted_old_enough(instance, interval):
                bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(context, instance.uuid)
                LOG.info('Reclaiming deleted instance', instance=instance)
                try:
                    self._delete_instance(context, instance, bdms)
                except Exception as e:
                    LOG.warning('Periodic reclaim failed to delete instance: %s', e, instance=instance)

    def _get_nodename(self, instance, refresh=False):
        """Helper method to get the name of the first available node
        on this host. This method should not be used with any operations
        on ironic instances since it does not handle multiple nodes.
        """
        node = self.driver.get_available_nodes(refresh=refresh)[0]
        LOG.debug('No node specified, defaulting to %s', node, instance=instance)
        return node

    def _update_available_resource_for_node(self, context, nodename, startup=False):
        try:
            self.rt.update_available_resource(context, nodename, startup=startup)
        except exception.ComputeHostNotFound:
            LOG.warning("Compute node '%s' not found in update_available_resource.", nodename)
        except exception.ReshapeFailed:
            with excutils.save_and_reraise_exception():
                LOG.critical('Resource provider data migration failed fatally during startup for node %s.', nodename)
        except exception.ReshapeNeeded:
            with excutils.save_and_reraise_exception():
                LOG.exception('ReshapeNeeded exception is unexpected here!')
        except Exception:
            LOG.exception('Error updating resources for node %(node)s.', {'node': nodename})

    @periodic_task.periodic_task(spacing=CONF.update_resources_interval)
    def update_available_resource(self, context, startup=False):
        """See driver.get_available_resource()

        Periodic process that keeps that the compute host's understanding of
        resource availability and usage in sync with the underlying hypervisor.

        :param context: security context
        :param startup: True if this is being called when the nova-compute
            service is starting, False otherwise.
        """
        try:
            nodenames = set(self.driver.get_available_nodes())
        except exception.VirtDriverNotReady:
            LOG.warning('Virt driver is not ready.')
            return
        compute_nodes_in_db = self._get_compute_nodes_in_db(context, nodenames, use_slave=True, startup=startup)
        for cn in compute_nodes_in_db:
            if cn.hypervisor_hostname not in nodenames:
                LOG.info('Deleting orphan compute node %(id)s hypervisor host is %(hh)s, nodes are %(nodes)s', {'id': cn.id, 'hh': cn.hypervisor_hostname, 'nodes': nodenames})
                cn.destroy()
                self.rt.remove_node(cn.hypervisor_hostname)
                try:
                    self.reportclient.delete_resource_provider(context, cn, cascade=True)
                except keystone_exception.ClientException as e:
                    LOG.error('Failed to delete compute node resource provider for compute node %s: %s', cn.uuid, str(e))
        for nodename in nodenames:
            self._update_available_resource_for_node(context, nodename, startup=startup)

    def _get_compute_nodes_in_db(self, context, nodenames, use_slave=False, startup=False):
        try:
            return objects.ComputeNodeList.get_all_by_host(context, self.host, use_slave=use_slave)
        except exception.NotFound:
            if nodenames:
                if startup:
                    LOG.warning('No compute node record found for host %s. If this is the first time this service is starting on this host, then you can ignore this warning.', self.host)
                else:
                    LOG.error('No compute node record for host %s', self.host)
            return []

    @periodic_task.periodic_task(spacing=CONF.running_deleted_instance_poll_interval, run_immediately=True)
    def _cleanup_running_deleted_instances(self, context):
        """Cleanup any instances which are erroneously still running after
        having been deleted.

        Valid actions to take are:

            1. noop - do nothing
            2. log - log which instances are erroneously running
            3. reap - shutdown and cleanup any erroneously running instances
            4. shutdown - power off *and disable* any erroneously running
                          instances

        The use-case for this cleanup task is: for various reasons, it may be
        possible for the database to show an instance as deleted but for that
        instance to still be running on a host machine (see bug
        https://bugs.launchpad.net/nova/+bug/911366).

        This cleanup task is a cross-hypervisor utility for finding these
        zombied instances and either logging the discrepancy (likely what you
        should do in production), or automatically reaping the instances (more
        appropriate for dev environments).
        """
        action = CONF.running_deleted_instance_action
        if action == 'noop':
            return
        with utils.temporary_mutation(context, read_deleted='yes'):
            try:
                instances = self._running_deleted_instances(context)
            except exception.VirtDriverNotReady:
                LOG.debug('Unable to check for running deleted instances at this time since the hypervisor is not ready.')
                return
            for instance in instances:
                if action == 'log':
                    LOG.warning("Detected instance with name label '%s' which is marked as DELETED but still present on host.", instance.name, instance=instance)
                elif action == 'shutdown':
                    LOG.info("Powering off instance with name label '%s' which is marked as DELETED but still present on host.", instance.name, instance=instance)
                    try:
                        self.driver.power_off(instance)
                    except Exception:
                        LOG.warning('Failed to power off instance', instance=instance, exc_info=True)
                elif action == 'reap':
                    LOG.info("Destroying instance with name label '%s' which is marked as DELETED but still present on host.", instance.name, instance=instance)
                    bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(context, instance.uuid, use_slave=True)
                    self.instance_events.clear_events_for_instance(instance)
                    try:
                        self._shutdown_instance(context, instance, bdms, notify=False)
                        self._cleanup_volumes(context, instance, bdms, detach=False)
                    except Exception as e:
                        LOG.warning('Periodic cleanup failed to delete instance: %s', e, instance=instance)
                else:
                    raise Exception(_("Unrecognized value '%s' for CONF.running_deleted_instance_action") % action)

    def _running_deleted_instances(self, context):
        """Returns a list of instances nova thinks is deleted,
        but the hypervisor thinks is still running.
        """
        timeout = CONF.running_deleted_instance_timeout
        filters = {'deleted': True, 'soft_deleted': False}
        instances = self._get_instances_on_driver(context, filters)
        return [i for i in instances if self._deleted_old_enough(i, timeout)]

    def _deleted_old_enough(self, instance, timeout):
        deleted_at = instance.deleted_at
        if deleted_at:
            deleted_at = deleted_at.replace(tzinfo=None)
        return not deleted_at or timeutils.is_older_than(deleted_at, timeout)

    @contextlib.contextmanager
    def _error_out_instance_on_exception(self, context, instance, instance_state=vm_states.ACTIVE):
        """Context manager to set instance.vm_state after some operation raises

        Used to handle NotImplementedError and InstanceFaultRollback errors
        and reset the instance vm_state and task_state. The vm_state is set
        to the $instance_state parameter and task_state is set to None.
        For all other types of exceptions, the vm_state is set to ERROR and
        the task_state is left unchanged (although most callers will have the
        @reverts_task_state decorator which will set the task_state to None).

        Re-raises the original exception *except* in the case of
        InstanceFaultRollback in which case the wrapped `inner_exception` is
        re-raised.

        :param context: The nova auth request context for the operation.
        :param instance: The instance to update. The vm_state will be set by
            this context manager when an exception is raised.
        :param instance_state: For NotImplementedError and
            InstanceFaultRollback this is the vm_state to set the instance to
            when handling one of those types of exceptions. By default the
            instance will be set to ACTIVE, but the caller should control this
            in case there have been no changes to the running state of the
            instance. For example, resizing a stopped server where prep_resize
            fails early and does not change the power state of the guest should
            not set the instance status to ACTIVE but remain STOPPED.
            This parameter is ignored for all other types of exceptions and the
            instance vm_state is set to ERROR.
        """
        instance_uuid = instance.uuid
        try:
            yield
        except (NotImplementedError, exception.InstanceFaultRollback) as error:
            with excutils.save_and_reraise_exception(reraise=False) as ctxt:
                LOG.info('Setting instance back to %(state)s after: %(error)s', {'state': instance_state, 'error': error}, instance_uuid=instance_uuid)
                self._instance_update(context, instance, vm_state=instance_state, task_state=None)
                if isinstance(error, exception.InstanceFaultRollback):
                    raise error.inner_exception
                ctxt.reraise = True
        except Exception:
            LOG.exception('Setting instance vm_state to ERROR', instance_uuid=instance_uuid)
            with excutils.save_and_reraise_exception():
                self._set_instance_obj_error_state(instance)

    def _process_instance_event(self, instance, event):
        _event = self.instance_events.pop_instance_event(instance, event)
        if _event:
            LOG.debug('Processing event %(event)s', {'event': event.key}, instance=instance)
            _event.send(event)
        elif event.name == 'network-vif-unplugged' and instance.task_state in (task_states.DELETING, task_states.MIGRATING):
            LOG.debug('Received event %s for instance with task_state %s.', event.key, instance.task_state, instance=instance)
        else:
            LOG.warning('Received unexpected event %(event)s for instance with vm_state %(vm_state)s and task_state %(task_state)s.', {'event': event.key, 'vm_state': instance.vm_state, 'task_state': instance.task_state}, instance=instance)

    def _process_instance_vif_deleted_event(self, context, instance, deleted_vif_id):
        network_info = instance.info_cache.network_info
        for (index, vif) in enumerate(network_info):
            if vif['id'] == deleted_vif_id:
                LOG.info('Neutron deleted interface %(intf)s; detaching it from the instance and deleting it from the info cache', {'intf': vif['id']}, instance=instance)
                profile = vif.get('profile', {}) or {}
                if profile.get('allocation'):
                    LOG.error('The bound port %(port_id)s is deleted in Neutron but the resource allocation on the resource provider %(rp_uuid)s is leaked until the server %(server_uuid)s is deleted.', {'port_id': vif['id'], 'rp_uuid': vif['profile']['allocation'], 'server_uuid': instance.uuid})
                del network_info[index]
                neutron.update_instance_cache_with_nw_info(self.network_api, context, instance, nw_info=network_info)
                try:
                    self.driver.detach_interface(context, instance, vif)
                except NotImplementedError:
                    pass
                except exception.NovaException as ex:
                    log_level = logging.DEBUG if isinstance(ex, exception.InstanceNotFound) else logging.WARNING
                    LOG.log(log_level, 'Detach interface failed, port_id=%(port_id)s, reason: %(msg)s', {'port_id': deleted_vif_id, 'msg': ex}, instance=instance)
                break

    @wrap_instance_event(prefix='compute')
    @wrap_instance_fault
    def extend_volume(self, context, instance, extended_volume_id):
        LOG.debug('Handling volume-extended event for volume %(vol)s', {'vol': extended_volume_id}, instance=instance)
        try:
            bdm = objects.BlockDeviceMapping.get_by_volume_and_instance(context, extended_volume_id, instance.uuid)
        except exception.NotFound:
            LOG.warning('Extend volume failed, volume %(vol)s is not attached to instance.', {'vol': extended_volume_id}, instance=instance)
            return
        LOG.info('Cinder extended volume %(vol)s; extending it to detect new size', {'vol': extended_volume_id}, instance=instance)
        volume = self.volume_api.get(context, bdm.volume_id)
        if bdm.connection_info is None:
            LOG.warning('Extend volume failed, attached volume %(vol)s has no connection_info', {'vol': extended_volume_id}, instance=instance)
            return
        connection_info = jsonutils.loads(bdm.connection_info)
        bdm.volume_size = volume['size']
        bdm.save()
        if not self.driver.capabilities.get('supports_extend_volume', False):
            raise exception.ExtendVolumeNotSupported()
        try:
            self.driver.extend_volume(context, connection_info, instance, bdm.volume_size * units.Gi)
        except Exception as ex:
            LOG.warning('Extend volume failed, volume_id=%(volume_id)s, reason: %(msg)s', {'volume_id': extended_volume_id, 'msg': ex}, instance=instance)
            raise

    @staticmethod
    def _is_state_valid_for_power_update_event(instance, target_power_state):
        """Check if the current state of the instance allows it to be
        a candidate for the power-update event.

        :param instance: The nova instance object.
        :param target_power_state: The desired target power state; this should
                                   either be "POWER_ON" or "POWER_OFF".
        :returns Boolean: True if the instance can be subjected to the
                          power-update event.
        """
        if target_power_state == external_event_obj.POWER_ON and instance.task_state is None and (instance.vm_state == vm_states.STOPPED) and (instance.power_state == power_state.SHUTDOWN) or (target_power_state == external_event_obj.POWER_OFF and instance.task_state is None and (instance.vm_state == vm_states.ACTIVE) and (instance.power_state == power_state.RUNNING)):
            return True
        return False

    @wrap_exception()
    @reverts_task_state
    @wrap_instance_event(prefix='compute')
    @wrap_instance_fault
    def power_update(self, context, instance, target_power_state):
        """Power update of an instance prompted by an external event.
        :param context: The API request context.
        :param instance: The nova instance object.
        :param target_power_state: The desired target power state;
                                   this should either be "POWER_ON" or
                                   "POWER_OFF".
        """

        @utils.synchronized(instance.uuid)
        def do_power_update():
            LOG.debug('Handling power-update event with target_power_state %s for instance', target_power_state, instance=instance)
            if not self._is_state_valid_for_power_update_event(instance, target_power_state):
                pow_state = fields.InstancePowerState.from_index(instance.power_state)
                LOG.info('The power-update %(tag)s event for instance %(uuid)s is a no-op since the instance is in vm_state %(vm_state)s, task_state %(task_state)s and power_state %(power_state)s.', {'tag': target_power_state, 'uuid': instance.uuid, 'vm_state': instance.vm_state, 'task_state': instance.task_state, 'power_state': pow_state})
                return
            LOG.debug('Trying to %s instance', target_power_state, instance=instance)
            if target_power_state == external_event_obj.POWER_ON:
                action = fields.NotificationAction.POWER_ON
                notification_name = 'power_on.'
                instance.task_state = task_states.POWERING_ON
            else:
                action = fields.NotificationAction.POWER_OFF
                notification_name = 'power_off.'
                instance.task_state = task_states.POWERING_OFF
                instance.progress = 0
            try:
                instance.save(expected_task_state=[None])
                self._notify_about_instance_usage(context, instance, notification_name + 'start')
                compute_utils.notify_about_instance_action(context, instance, self.host, action=action, phase=fields.NotificationPhase.START)
                self.driver.power_update_event(instance, target_power_state)
                self._notify_about_instance_usage(context, instance, notification_name + 'end')
                compute_utils.notify_about_instance_action(context, instance, self.host, action=action, phase=fields.NotificationPhase.END)
            except exception.UnexpectedTaskStateError as e:
                LOG.info('The power-update event was possibly preempted: %s ', e.format_message(), instance=instance)
                return
        do_power_update()

    @wrap_exception()
    def external_instance_event(self, context, instances, events):
        for event in events:
            instance = [inst for inst in instances if inst.uuid == event.instance_uuid][0]
            LOG.debug('Received event %(event)s', {'event': event.key}, instance=instance)
            if event.name == 'network-changed':
                try:
                    LOG.debug('Refreshing instance network info cache due to event %s.', event.key, instance=instance)
                    self.network_api.get_instance_nw_info(context, instance, refresh_vif_id=event.tag)
                except exception.NotFound as e:
                    LOG.info('Failed to process external instance event %(event)s due to: %(error)s', {'event': event.key, 'error': str(e)}, instance=instance)
            elif event.name == 'network-vif-deleted':
                try:
                    self._process_instance_vif_deleted_event(context, instance, event.tag)
                except exception.NotFound as e:
                    LOG.info('Failed to process external instance event %(event)s due to: %(error)s', {'event': event.key, 'error': str(e)}, instance=instance)
            elif event.name == 'volume-extended':
                self.extend_volume(context, instance, event.tag)
            elif event.name == 'power-update':
                self.power_update(context, instance, event.tag)
            else:
                self._process_instance_event(instance, event)

    @periodic_task.periodic_task(spacing=CONF.image_cache.manager_interval, external_process_ok=True)
    def _run_image_cache_manager_pass(self, context):
        """Run a single pass of the image cache manager."""
        if not self.driver.capabilities.get('has_imagecache', False):
            return
        storage_users.register_storage_use(CONF.instances_path, CONF.host)
        nodes = storage_users.get_storage_users(CONF.instances_path)
        filters = {'deleted': False, 'soft_deleted': True, 'host': nodes}
        filtered_instances = objects.InstanceList.get_by_filters(context, filters, expected_attrs=[], use_slave=True)
        self.driver.manage_image_cache(context, filtered_instances)

    def cache_images(self, context, image_ids):
        """Ask the virt driver to pre-cache a set of base images.

        :param context: The RequestContext
        :param image_ids: The image IDs to be cached
        :return: A dict, keyed by image-id where the values are one of:
                 'cached' if the image was downloaded,
                 'existing' if the image was already in the cache,
                 'unsupported' if the virt driver does not support caching,
                 'error' if the virt driver raised an exception.
        """
        results = {}
        LOG.info('Caching %i image(s) by request', len(image_ids))
        for image_id in image_ids:
            try:
                cached = self.driver.cache_image(context, image_id)
                if cached:
                    results[image_id] = 'cached'
                else:
                    results[image_id] = 'existing'
            except NotImplementedError:
                LOG.warning('Virt driver does not support image pre-caching; ignoring request')
                results[image_id] = 'unsupported'
            except Exception as e:
                results[image_id] = 'error'
                LOG.error('Failed to cache image %(image_id)s: %(err)s', {'image_id': image_id, 'err': e})
        return results

    @periodic_task.periodic_task(spacing=CONF.instance_delete_interval)
    def _run_pending_deletes(self, context):
        """Retry any pending instance file deletes."""
        LOG.debug('Cleaning up deleted instances')
        filters = {'deleted': True, 'soft_deleted': False, 'host': CONF.host, 'cleaned': False}
        attrs = ['system_metadata']
        with utils.temporary_mutation(context, read_deleted='yes'):
            instances = objects.InstanceList.get_by_filters(context, filters, expected_attrs=attrs, use_slave=True)
        LOG.debug('There are %d instances to clean', len(instances))
        for instance in instances:
            attempts = int(instance.system_metadata.get('clean_attempts', '0'))
            LOG.debug('Instance has had %(attempts)s of %(max)s cleanup attempts', {'attempts': attempts, 'max': CONF.maximum_instance_delete_attempts}, instance=instance)
            if attempts < CONF.maximum_instance_delete_attempts:
                success = self.driver.delete_instance_files(instance)
                instance.system_metadata['clean_attempts'] = str(attempts + 1)
                if success:
                    instance.cleaned = True
                with utils.temporary_mutation(context, read_deleted='yes'):
                    instance.save()

    @periodic_task.periodic_task(spacing=CONF.instance_delete_interval)
    def _cleanup_incomplete_migrations(self, context):
        """Cleanup on failed resize/revert-resize operation and
        failed rollback live migration operation.

        During resize/revert-resize operation, or after a failed rollback
        live migration operation, if that instance gets deleted then instance
        files might remain either on source or destination compute node and
        other specific resources might not be cleaned up because of the race
        condition.
        """
        LOG.debug('Cleaning up deleted instances with incomplete migration ')
        migration_filters = {'host': CONF.host, 'status': 'error'}
        migrations = objects.MigrationList.get_by_filters(context, migration_filters)
        if not migrations:
            return
        inst_uuid_from_migrations = set([migration.instance_uuid for migration in migrations])
        inst_filters = {'deleted': True, 'soft_deleted': False, 'uuid': inst_uuid_from_migrations}
        attrs = ['info_cache', 'security_groups', 'system_metadata']
        with utils.temporary_mutation(context, read_deleted='yes'):
            instances = objects.InstanceList.get_by_filters(context, inst_filters, expected_attrs=attrs, use_slave=True)
        for instance in instances:
            if instance.host == CONF.host:
                continue
            for migration in migrations:
                if instance.uuid != migration.instance_uuid:
                    continue
                self.driver.delete_instance_files(instance)
                revert = True if migration.source_compute == CONF.host else False
                with instance.mutated_migration_context(revert=revert):
                    self.driver.cleanup_lingering_instance_resources(instance)
                try:
                    migration.status = 'failed'
                    migration.save()
                except exception.MigrationNotFound:
                    LOG.warning('Migration %s is not found.', migration.id, instance=instance)
                break

    @messaging.expected_exceptions(exception.InstanceQuiesceNotSupported, exception.QemuGuestAgentNotEnabled, exception.NovaException, NotImplementedError)
    @wrap_exception()
    def quiesce_instance(self, context, instance):
        """Quiesce an instance on this host."""
        context = context.elevated()
        image_meta = objects.ImageMeta.from_instance(instance)
        self.driver.quiesce(context, instance, image_meta)

    def _wait_for_snapshots_completion(self, context, mapping):
        for mapping_dict in mapping:
            if mapping_dict.get('source_type') == 'snapshot':

                def _wait_snapshot():
                    snapshot = self.volume_api.get_snapshot(context, mapping_dict['snapshot_id'])
                    if snapshot.get('status') != 'creating':
                        raise loopingcall.LoopingCallDone()
                timer = loopingcall.FixedIntervalLoopingCall(_wait_snapshot)
                timer.start(interval=0.5).wait()

    @messaging.expected_exceptions(exception.InstanceQuiesceNotSupported, exception.QemuGuestAgentNotEnabled, exception.NovaException, NotImplementedError)
    @wrap_exception()
    def unquiesce_instance(self, context, instance, mapping=None):
        """Unquiesce an instance on this host.

        If snapshots' image mapping is provided, it waits until snapshots are
        completed before unqueiscing.
        """
        context = context.elevated()
        if mapping:
            try:
                self._wait_for_snapshots_completion(context, mapping)
            except Exception as error:
                LOG.exception('Exception while waiting completion of volume snapshots: %s', error, instance=instance)
        image_meta = objects.ImageMeta.from_instance(instance)
        self.driver.unquiesce(context, instance, image_meta)

    @periodic_task.periodic_task(spacing=CONF.instance_delete_interval)
    def _cleanup_expired_console_auth_tokens(self, context):
        """Remove all expired console auth tokens.

        Console authorization tokens and their connection data are stored
        in the database when a user asks for a console connection to an
        instance. After a time they expire. We periodically remove any expired
        tokens from the database.
        """
        objects.ConsoleAuthToken.clean_expired_console_auths(context)

    def _claim_pci_for_instance_vifs(self, ctxt, instance):
        """Claim PCI devices for the instance's VIFs on the compute node

        :param ctxt: Context
        :param instance: Instance object
        :return: <port ID: PciDevice> mapping for the VIFs that yielded a
                PCI claim on the compute node
        """
        pci_req_id_to_port_id = {}
        pci_reqs = []
        port_id_to_pci_dev = {}
        for vif in instance.get_network_info():
            pci_req = pci_req_module.get_instance_pci_request_from_vif(ctxt, instance, vif)
            if pci_req:
                pci_req_id_to_port_id[pci_req.request_id] = vif['id']
                pci_reqs.append(pci_req)
        if pci_reqs:
            vif_pci_requests = objects.InstancePCIRequests(requests=pci_reqs, instance_uuid=instance.uuid)
            dest_topo = None
            if instance.migration_context:
                dest_topo = instance.migration_context.new_numa_topology
            claimed_pci_devices_objs = self.rt.claim_pci_devices(ctxt, vif_pci_requests, dest_topo)
            for pci_dev in claimed_pci_devices_objs:
                LOG.debug('PCI device: %s Claimed on destination node', pci_dev.address)
                port_id = pci_req_id_to_port_id[pci_dev.request_id]
                port_id_to_pci_dev[port_id] = pci_dev
        return port_id_to_pci_dev

    def _update_migrate_vifs_profile_with_pci(self, migrate_vifs, port_id_to_pci_dev):
        """Update migrate vifs profile with the claimed PCI devices

        :param migrate_vifs: list of VIFMigrateData objects
        :param port_id_to_pci_dev: a <port_id: PciDevice> mapping
        :return: None.
        """
        for mig_vif in migrate_vifs:
            port_id = mig_vif.port_id
            if port_id not in port_id_to_pci_dev:
                continue
            pci_dev = port_id_to_pci_dev[port_id]
            profile = copy.deepcopy(mig_vif.source_vif['profile'])
            profile['pci_slot'] = pci_dev.address
            profile['pci_vendor_info'] = ':'.join([pci_dev.vendor_id, pci_dev.product_id])
            mig_vif.profile = profile
            LOG.debug('Updating migrate VIF profile for port %(port_id)s:%(profile)s', {'port_id': port_id, 'profile': profile})

@p.trace_cls('_ComputeV5Proxy')
class _ComputeV5Proxy(object):
    target = messaging.Target(version='5.13')

    def __init__(self, manager):
        self.manager = manager

    def __getattr__(self, name):
        return getattr(self.manager, name)

    def pre_live_migration(self, context, instance, block_migration, disk, migrate_data):
        return self.manager.pre_live_migration(context, instance, disk, migrate_data)

    def prep_resize(self, context, image, instance, instance_type, request_spec, filter_properties, node, clean_shutdown, migration, host_list):
        if not isinstance(request_spec, objects.RequestSpec):
            request_spec = objects.RequestSpec.from_primitives(context, request_spec, filter_properties)
        self.manager.prep_resize(context, image, instance, instance_type, request_spec, filter_properties, node, clean_shutdown, migration, host_list)

    def resize_instance(self, context, instance, image, migration, instance_type, clean_shutdown, request_spec=None):
        self.manager.resize_instance(context, instance, image, migration, instance_type, clean_shutdown, request_spec)

    def finish_resize(self, context, disk_info, image, instance, migration, request_spec=None):
        self.manager.finish_resize(context, disk_info, image, instance, migration, request_spec)

    def revert_resize(self, context, instance, migration, request_spec=None):
        self.manager.revert_resize(context, instance, migration, request_spec)

    def finish_revert_resize(self, context, instance, migration, request_spec=None):
        self.manager.finish_revert_resize(context, instance, migration, request_spec)

    def unshelve_instance(self, context, instance, image, filter_properties, node, request_spec=None, accel_uuids=None):
        self.manager.unshelve_instance(context, instance, image, filter_properties, node, request_spec, accel_uuids or [])

    def check_can_live_migrate_destination(self, ctxt, instance, block_migration, disk_over_commit, migration=None, limits=None):
        return self.manager.check_can_live_migrate_destination(ctxt, instance, block_migration, disk_over_commit, migration, limits)

    def build_and_run_instance(self, context, instance, image, request_spec, filter_properties, admin_password=None, injected_files=None, requested_networks=None, security_groups=None, block_device_mapping=None, node=None, limits=None, host_list=None, accel_uuids=None):
        self.manager.build_and_run_instance(context, instance, image, request_spec, filter_properties, accel_uuids, admin_password, injected_files, requested_networks, security_groups, block_device_mapping, node, limits, host_list)

    def rebuild_instance(self, context, instance, orig_image_ref, image_ref, injected_files, new_pass, orig_sys_metadata, bdms, recreate, on_shared_storage, preserve_ephemeral, migration, scheduled_node, limits, request_spec, accel_uuids=None):
        self.manager.rebuild_instance(context, instance, orig_image_ref, image_ref, injected_files, new_pass, orig_sys_metadata, bdms, recreate, on_shared_storage, preserve_ephemeral, migration, scheduled_node, limits, request_spec, accel_uuids)

    def shelve_instance(self, context, instance, image_id, clean_shutdown, accel_uuids=None):
        self.manager.shelve_instance(context, instance, image_id, clean_shutdown, accel_uuids)

    def shelve_offload_instance(self, context, instance, clean_shutdown, accel_uuids=None):
        self.manager.shelve_offload_instance(context, instance, clean_shutdown, accel_uuids)

    def prep_snapshot_based_resize_at_dest(self, ctxt, instance, flavor, nodename, migration, limits, request_spec):
        return self.manager.prep_snapshot_based_resize_at_dest(ctxt, instance, flavor, nodename, migration, limits)

    def finish_snapshot_based_resize_at_dest(self, ctxt, instance, migration, snapshot_id, request_spec):
        self.manager.finish_snapshot_based_resize_at_dest(ctxt, instance, migration, snapshot_id)

    def check_instance_shared_storage(self, ctxt, instance, data):
        return self.manager.check_instance_shared_storage(ctxt, data)