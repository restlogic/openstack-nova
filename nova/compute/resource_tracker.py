from bees import profiler as p
'\nTrack resources like memory and disk for a compute host.  Provides the\nscheduler with useful information about availability through the ComputeNode\nmodel.\n'
import collections
import copy
from keystoneauth1 import exceptions as ks_exc
import os_traits
from oslo_log import log as logging
from oslo_serialization import jsonutils
from oslo_utils import excutils
import retrying
from nova.compute import claims
from nova.compute import monitors
from nova.compute import provider_config
from nova.compute import stats as compute_stats
from nova.compute import task_states
from nova.compute import utils as compute_utils
from nova.compute import vm_states
import nova.conf
from nova import exception
from nova.i18n import _
from nova import objects
from nova.objects import base as obj_base
from nova.objects import fields
from nova.objects import migration as migration_obj
from nova.pci import manager as pci_manager
from nova.pci import request as pci_request
from nova import rpc
from nova.scheduler.client import report
from nova import utils
from nova.virt import hardware
CONF = nova.conf.CONF
LOG = logging.getLogger(__name__)
COMPUTE_RESOURCE_SEMAPHORE = 'compute_resources'

@p.trace('_instance_in_resize_state')
def _instance_in_resize_state(instance):
    """Returns True if the instance is in one of the resizing states.

    :param instance: `nova.objects.Instance` object
    """
    vm = instance.vm_state
    task = instance.task_state
    if vm == vm_states.RESIZED:
        return True
    if vm in [vm_states.ACTIVE, vm_states.STOPPED] and task in task_states.resizing_states + task_states.rebuild_states:
        return True
    return False

@p.trace('_instance_is_live_migrating')
def _instance_is_live_migrating(instance):
    vm = instance.vm_state
    task = instance.task_state
    if task == task_states.MIGRATING and vm in [vm_states.ACTIVE, vm_states.PAUSED]:
        return True
    return False

@p.trace_cls('ResourceTracker')
class ResourceTracker(object):
    """Compute helper class for keeping track of resource usage as instances
    are built and destroyed.
    """

    def __init__(self, host, driver, reportclient=None):
        self.host = host
        self.driver = driver
        self.pci_tracker = None
        self.compute_nodes = {}
        self.stats = collections.defaultdict(compute_stats.Stats)
        self.tracked_instances = set()
        self.tracked_migrations = {}
        self.is_bfv = {}
        monitor_handler = monitors.MonitorHandler(self)
        self.monitors = monitor_handler.monitors
        self.old_resources = collections.defaultdict(objects.ComputeNode)
        self.reportclient = reportclient or report.SchedulerReportClient()
        self.ram_allocation_ratio = CONF.ram_allocation_ratio
        self.cpu_allocation_ratio = CONF.cpu_allocation_ratio
        self.disk_allocation_ratio = CONF.disk_allocation_ratio
        self.provider_tree = None
        self.assigned_resources = collections.defaultdict(lambda : collections.defaultdict(set))
        self.provider_configs = provider_config.get_provider_configs(CONF.compute.provider_config_location)
        self.absent_providers = set()

    @utils.synchronized(COMPUTE_RESOURCE_SEMAPHORE, fair=True)
    def instance_claim(self, context, instance, nodename, allocations, limits=None):
        """Indicate that some resources are needed for an upcoming compute
        instance build operation.

        This should be called before the compute node is about to perform
        an instance build operation that will consume additional resources.

        :param context: security context
        :param instance: instance to reserve resources for.
        :type instance: nova.objects.instance.Instance object
        :param nodename: The Ironic nodename selected by the scheduler
        :param allocations: The placement allocation records for the instance.
        :param limits: Dict of oversubscription limits for memory, disk,
                       and CPUs.
        :returns: A Claim ticket representing the reserved resources.  It can
                  be used to revert the resource usage if an error occurs
                  during the instance build.
        """
        if self.disabled(nodename):
            self._set_instance_host_and_node(instance, nodename)
            return claims.NopClaim()
        if instance.host:
            LOG.warning('Host field should not be set on the instance until resources have been claimed.', instance=instance)
        if instance.node:
            LOG.warning('Node field should not be set on the instance until resources have been claimed.', instance=instance)
        cn = self.compute_nodes[nodename]
        pci_requests = instance.pci_requests
        claim = claims.Claim(context, instance, nodename, self, cn, pci_requests, limits=limits)
        instance_numa_topology = claim.claimed_numa_topology
        instance.numa_topology = instance_numa_topology
        self._set_instance_host_and_node(instance, nodename)
        if self.pci_tracker:
            self.pci_tracker.claim_instance(context, pci_requests, instance_numa_topology)
        claimed_resources = self._claim_resources(allocations)
        instance.resources = claimed_resources
        self._update_usage_from_instance(context, instance, nodename)
        elevated = context.elevated()
        self._update(elevated, cn)
        return claim

    @utils.synchronized(COMPUTE_RESOURCE_SEMAPHORE, fair=True)
    def rebuild_claim(self, context, instance, nodename, allocations, limits=None, image_meta=None, migration=None):
        """Create a claim for a rebuild operation."""
        instance_type = instance.flavor
        return self._move_claim(context, instance, instance_type, nodename, migration, allocations, move_type=fields.MigrationType.EVACUATION, image_meta=image_meta, limits=limits)

    @utils.synchronized(COMPUTE_RESOURCE_SEMAPHORE, fair=True)
    def resize_claim(self, context, instance, instance_type, nodename, migration, allocations, image_meta=None, limits=None):
        """Create a claim for a resize or cold-migration move.

        Note that this code assumes ``instance.new_flavor`` is set when
        resizing with a new flavor.
        """
        return self._move_claim(context, instance, instance_type, nodename, migration, allocations, image_meta=image_meta, limits=limits)

    @utils.synchronized(COMPUTE_RESOURCE_SEMAPHORE, fair=True)
    def live_migration_claim(self, context, instance, nodename, migration, limits, allocs):
        """Builds a MoveClaim for a live migration.

        :param context: The request context.
        :param instance: The instance being live migrated.
        :param nodename: The nodename of the destination host.
        :param migration: The Migration object associated with this live
                          migration.
        :param limits: A SchedulerLimits object from when the scheduler
                       selected the destination host.
        :param allocs: The placement allocation records for the instance.
        :returns: A MoveClaim for this live migration.
        """
        instance_type = instance.flavor
        image_meta = instance.image_meta
        return self._move_claim(context, instance, instance_type, nodename, migration, allocs, move_type=fields.MigrationType.LIVE_MIGRATION, image_meta=image_meta, limits=limits)

    def _move_claim(self, context, instance, new_instance_type, nodename, migration, allocations, move_type=None, image_meta=None, limits=None):
        """Indicate that resources are needed for a move to this host.

        Move can be either a migrate/resize, live-migrate or an
        evacuate/rebuild operation.

        :param context: security context
        :param instance: instance object to reserve resources for
        :param new_instance_type: new instance_type being resized to
        :param nodename: The Ironic nodename selected by the scheduler
        :param migration: A migration object if one was already created
                          elsewhere for this operation (otherwise None)
        :param allocations: the placement allocation records.
        :param move_type: move type - can be one of 'migration', 'resize',
                         'live-migration', 'evacuate'
        :param image_meta: instance image metadata
        :param limits: Dict of oversubscription limits for memory, disk,
        and CPUs
        :returns: A Claim ticket representing the reserved resources.  This
        should be turned into finalize  a resource claim or free
        resources after the compute operation is finished.
        """
        image_meta = image_meta or {}
        if migration:
            self._claim_existing_migration(migration, nodename)
        else:
            migration = self._create_migration(context, instance, new_instance_type, nodename, move_type)
        if self.disabled(nodename):
            return claims.NopClaim(migration=migration)
        cn = self.compute_nodes[nodename]
        new_pci_requests = pci_request.get_pci_requests_from_flavor(new_instance_type)
        new_pci_requests.instance_uuid = instance.uuid
        if instance.pci_requests:
            for request in instance.pci_requests.requests:
                if request.source == objects.InstancePCIRequest.NEUTRON_PORT:
                    new_pci_requests.requests.append(request)
        claim = claims.MoveClaim(context, instance, nodename, new_instance_type, image_meta, self, cn, new_pci_requests, migration, limits=limits)
        claimed_pci_devices_objs = []
        if self.pci_tracker and (not migration.is_live_migration):
            claimed_pci_devices_objs = self.pci_tracker.claim_instance(context, new_pci_requests, claim.claimed_numa_topology)
        claimed_pci_devices = objects.PciDeviceList(objects=claimed_pci_devices_objs)
        claimed_resources = self._claim_resources(allocations)
        old_resources = instance.resources
        mig_context = objects.MigrationContext(context=context, instance_uuid=instance.uuid, migration_id=migration.id, old_numa_topology=instance.numa_topology, new_numa_topology=claim.claimed_numa_topology, old_pci_devices=instance.pci_devices, new_pci_devices=claimed_pci_devices, old_pci_requests=instance.pci_requests, new_pci_requests=new_pci_requests, old_resources=old_resources, new_resources=claimed_resources)
        instance.migration_context = mig_context
        instance.save()
        self._update_usage_from_migration(context, instance, migration, nodename)
        elevated = context.elevated()
        self._update(elevated, cn)
        return claim

    def _create_migration(self, context, instance, new_instance_type, nodename, move_type=None):
        """Create a migration record for the upcoming resize.  This should
        be done while the COMPUTE_RESOURCES_SEMAPHORE is held so the resource
        claim will not be lost if the audit process starts.
        """
        migration = objects.Migration(context=context.elevated())
        migration.dest_compute = self.host
        migration.dest_node = nodename
        migration.dest_host = self.driver.get_host_ip_addr()
        migration.old_instance_type_id = instance.flavor.id
        migration.new_instance_type_id = new_instance_type.id
        migration.status = 'pre-migrating'
        migration.instance_uuid = instance.uuid
        migration.source_compute = instance.host
        migration.source_node = instance.node
        if move_type:
            migration.migration_type = move_type
        else:
            migration.migration_type = migration_obj.determine_migration_type(migration)
        migration.create()
        return migration

    def _claim_existing_migration(self, migration, nodename):
        """Make an existing migration record count for resource tracking.

        If a migration record was created already before the request made
        it to this compute host, only set up the migration so it's included in
        resource tracking. This should be done while the
        COMPUTE_RESOURCES_SEMAPHORE is held.
        """
        migration.dest_compute = self.host
        migration.dest_node = nodename
        migration.dest_host = self.driver.get_host_ip_addr()
        if not migration.is_live_migration:
            migration.status = 'pre-migrating'
        migration.save()

    def _claim_resources(self, allocations):
        """Claim resources according to assigned resources from allocations
        and available resources in provider tree
        """
        if not allocations:
            return None
        claimed_resources = []
        for (rp_uuid, alloc_dict) in allocations.items():
            try:
                provider_data = self.provider_tree.data(rp_uuid)
            except ValueError:
                LOG.debug('Skip claiming resources of provider %(rp_uuid)s, since the provider UUIDs are not in provider tree.', {'rp_uuid': rp_uuid})
                continue
            for (rc, amount) in alloc_dict['resources'].items():
                if rc not in provider_data.resources:
                    continue
                assigned = self.assigned_resources[rp_uuid][rc]
                free = provider_data.resources[rc] - assigned
                if amount > len(free):
                    reason = _('Needed %(amount)d units of resource class %(rc)s, but %(avail)d are available.') % {'amount': amount, 'rc': rc, 'avail': len(free)}
                    raise exception.ComputeResourcesUnavailable(reason=reason)
                for i in range(amount):
                    claimed_resources.append(free.pop())
        if claimed_resources:
            self._add_assigned_resources(claimed_resources)
            return objects.ResourceList(objects=claimed_resources)

    def _populate_assigned_resources(self, context, instance_by_uuid):
        """Populate self.assigned_resources organized by resource class and
        reource provider uuid, which is as following format:
        {
        $RP_UUID: {
            $RESOURCE_CLASS: [objects.Resource, ...],
            $RESOURCE_CLASS: [...]},
        ...}
        """
        resources = []
        for mig in self.tracked_migrations.values():
            mig_ctx = mig.instance.migration_context
            if not mig_ctx:
                continue
            if mig.source_compute == self.host and 'old_resources' in mig_ctx:
                resources.extend(mig_ctx.old_resources or [])
            if mig.dest_compute == self.host and 'new_resources' in mig_ctx:
                resources.extend(mig_ctx.new_resources or [])
        for uuid in self.tracked_instances:
            resources.extend(instance_by_uuid[uuid].resources or [])
        self.assigned_resources.clear()
        self._add_assigned_resources(resources)

    def _check_resources(self, context):
        """Check if there are assigned resources not found in provider tree"""
        notfound = set()
        for rp_uuid in self.assigned_resources:
            provider_data = self.provider_tree.data(rp_uuid)
            for (rc, assigned) in self.assigned_resources[rp_uuid].items():
                notfound |= assigned - provider_data.resources[rc]
        if not notfound:
            return
        resources = [(res.identifier, res.resource_class) for res in notfound]
        reason = _('The following resources are assigned to instances, but were not listed in the configuration: %s Please check if this will influence your instances, and restore your configuration if necessary') % resources
        raise exception.AssignedResourceNotFound(reason=reason)

    def _release_assigned_resources(self, resources):
        """Remove resources from self.assigned_resources."""
        if not resources:
            return
        for resource in resources:
            rp_uuid = resource.provider_uuid
            rc = resource.resource_class
            try:
                self.assigned_resources[rp_uuid][rc].remove(resource)
            except KeyError:
                LOG.warning('Release resource %(rc)s: %(id)s of provider %(rp_uuid)s, not tracked in ResourceTracker.assigned_resources.', {'rc': rc, 'id': resource.identifier, 'rp_uuid': rp_uuid})

    def _add_assigned_resources(self, resources):
        """Add resources to self.assigned_resources"""
        if not resources:
            return
        for resource in resources:
            rp_uuid = resource.provider_uuid
            rc = resource.resource_class
            self.assigned_resources[rp_uuid][rc].add(resource)

    def _set_instance_host_and_node(self, instance, nodename):
        """Tag the instance as belonging to this host.  This should be done
        while the COMPUTE_RESOURCES_SEMAPHORE is held so the resource claim
        will not be lost if the audit process starts.
        """
        instance.host = self.host
        instance.launched_on = self.host
        instance.node = nodename
        instance.save()

    def _unset_instance_host_and_node(self, instance):
        """Untag the instance so it no longer belongs to the host.

        This should be done while the COMPUTE_RESOURCES_SEMAPHORE is held so
        the resource claim will not be lost if the audit process starts.
        """
        instance.host = None
        instance.node = None
        instance.save()

    @utils.synchronized(COMPUTE_RESOURCE_SEMAPHORE, fair=True)
    def abort_instance_claim(self, context, instance, nodename):
        """Remove usage from the given instance."""
        self._update_usage_from_instance(context, instance, nodename, is_removed=True)
        instance.clear_numa_topology()
        self._unset_instance_host_and_node(instance)
        self._update(context.elevated(), self.compute_nodes[nodename])

    def _drop_pci_devices(self, instance, nodename, prefix):
        if self.pci_tracker:
            pci_devices = self._get_migration_context_resource('pci_devices', instance, prefix=prefix)
            if pci_devices:
                for pci_device in pci_devices:
                    self.pci_tracker.free_device(pci_device, instance)
                dev_pools_obj = self.pci_tracker.stats.to_device_pools_obj()
                self.compute_nodes[nodename].pci_device_pools = dev_pools_obj

    @utils.synchronized(COMPUTE_RESOURCE_SEMAPHORE, fair=True)
    def drop_move_claim_at_source(self, context, instance, migration):
        """Drop a move claim after confirming a resize or cold migration."""
        migration.status = 'confirmed'
        migration.save()
        self._drop_move_claim(context, instance, migration.source_node, instance.old_flavor, prefix='old_')
        instance.drop_migration_context()

    @utils.synchronized(COMPUTE_RESOURCE_SEMAPHORE, fair=True)
    def drop_move_claim_at_dest(self, context, instance, migration):
        """Drop a move claim after reverting a resize or cold migration."""
        migration.status = 'reverted'
        migration.save()
        self._drop_move_claim(context, instance, migration.dest_node, instance.new_flavor, prefix='new_')
        instance.revert_migration_context()
        instance.save(expected_task_state=[task_states.RESIZE_REVERTING])

    @utils.synchronized(COMPUTE_RESOURCE_SEMAPHORE, fair=True)
    def drop_move_claim(self, context, instance, nodename, instance_type=None, prefix='new_'):
        self._drop_move_claim(context, instance, nodename, instance_type, prefix='new_')

    def _drop_move_claim(self, context, instance, nodename, instance_type=None, prefix='new_'):
        """Remove usage for an incoming/outgoing migration.

        :param context: Security context.
        :param instance: The instance whose usage is to be removed.
        :param nodename: Host on which to remove usage. If the migration
                         completed successfully, this is normally the source.
                         If it did not complete successfully (failed or
                         reverted), this is normally the destination.
        :param instance_type: The flavor that determines the usage to remove.
                              If the migration completed successfully, this is
                              the old flavor to be removed from the source. If
                              the migration did not complete successfully, this
                              is the new flavor to be removed from the
                              destination.
        :param prefix: Prefix to use when accessing migration context
                       attributes. 'old_' or 'new_', with 'new_' being the
                       default.
        """
        if instance['uuid'] in self.tracked_migrations:
            migration = self.tracked_migrations.pop(instance['uuid'])
            if not instance_type:
                instance_type = self._get_instance_type(instance, prefix, migration)
        elif instance['uuid'] in self.tracked_instances:
            self.tracked_instances.remove(instance['uuid'])
        if instance_type is not None:
            numa_topology = self._get_migration_context_resource('numa_topology', instance, prefix=prefix)
            usage = self._get_usage_dict(instance_type, instance, numa_topology=numa_topology)
            self._drop_pci_devices(instance, nodename, prefix)
            resources = self._get_migration_context_resource('resources', instance, prefix=prefix)
            self._release_assigned_resources(resources)
            self._update_usage(usage, nodename, sign=-1)
            ctxt = context.elevated()
            self._update(ctxt, self.compute_nodes[nodename])

    @utils.synchronized(COMPUTE_RESOURCE_SEMAPHORE, fair=True)
    def update_usage(self, context, instance, nodename):
        """Update the resource usage and stats after a change in an
        instance
        """
        if self.disabled(nodename):
            return
        uuid = instance['uuid']
        if uuid in self.tracked_instances:
            self._update_usage_from_instance(context, instance, nodename)
            self._update(context.elevated(), self.compute_nodes[nodename])

    def disabled(self, nodename):
        return nodename not in self.compute_nodes or not self.driver.node_is_available(nodename)

    def _check_for_nodes_rebalance(self, context, resources, nodename):
        """Check if nodes rebalance has happened.

        The ironic driver maintains a hash ring mapping bare metal nodes
        to compute nodes. If a compute dies, the hash ring is rebuilt, and
        some of its bare metal nodes (more precisely, those not in ACTIVE
        state) are assigned to other computes.

        This method checks for this condition and adjusts the database
        accordingly.

        :param context: security context
        :param resources: initial values
        :param nodename: node name
        :returns: True if a suitable compute node record was found, else False
        """
        if not self.driver.rebalances_nodes:
            return False
        cn_candidates = objects.ComputeNodeList.get_by_hypervisor(context, nodename)
        if len(cn_candidates) == 1:
            cn = cn_candidates[0]
            LOG.info('ComputeNode %(name)s moving from %(old)s to %(new)s', {'name': nodename, 'old': cn.host, 'new': self.host})
            cn.host = self.host
            self.compute_nodes[nodename] = cn
            self._copy_resources(cn, resources)
            self._setup_pci_tracker(context, cn, resources)
            self._update(context, cn)
            return True
        elif len(cn_candidates) > 1:
            LOG.error('Found more than one ComputeNode for nodename %s. Please clean up the orphaned ComputeNode records in your DB.', nodename)
        return False

    def _init_compute_node(self, context, resources):
        """Initialize the compute node if it does not already exist.

        The resource tracker will be inoperable if compute_node
        is not defined. The compute_node will remain undefined if
        we fail to create it or if there is no associated service
        registered.

        If this method has to create a compute node it needs initial
        values - these come from resources.

        :param context: security context
        :param resources: initial values
        :returns: True if a new compute_nodes table record was created,
            False otherwise
        """
        nodename = resources['hypervisor_hostname']
        if nodename in self.compute_nodes:
            cn = self.compute_nodes[nodename]
            self._copy_resources(cn, resources)
            self._setup_pci_tracker(context, cn, resources)
            return False
        cn = self._get_compute_node(context, nodename)
        if cn:
            self.compute_nodes[nodename] = cn
            self._copy_resources(cn, resources)
            self._setup_pci_tracker(context, cn, resources)
            return False
        if self._check_for_nodes_rebalance(context, resources, nodename):
            return False
        cn = objects.ComputeNode(context)
        cn.host = self.host
        self._copy_resources(cn, resources, initial=True)
        cn.create()
        self.compute_nodes[nodename] = cn
        LOG.info('Compute node record created for %(host)s:%(node)s with uuid: %(uuid)s', {'host': self.host, 'node': nodename, 'uuid': cn.uuid})
        self._setup_pci_tracker(context, cn, resources)
        return True

    def _setup_pci_tracker(self, context, compute_node, resources):
        if not self.pci_tracker:
            self.pci_tracker = pci_manager.PciDevTracker(context, compute_node)
            if 'pci_passthrough_devices' in resources:
                dev_json = resources.pop('pci_passthrough_devices')
                self.pci_tracker.update_devices_from_hypervisor_resources(dev_json)
            dev_pools_obj = self.pci_tracker.stats.to_device_pools_obj()
            compute_node.pci_device_pools = dev_pools_obj

    def _copy_resources(self, compute_node, resources, initial=False):
        """Copy resource values to supplied compute_node."""
        nodename = resources['hypervisor_hostname']
        stats = self.stats[nodename]
        prev_failed_builds = stats.get('failed_builds', 0)
        stats.clear()
        stats['failed_builds'] = prev_failed_builds
        stats.digest_stats(resources.get('stats'))
        compute_node.stats = stats
        for res in ('cpu', 'disk', 'ram'):
            attr = '%s_allocation_ratio' % res
            if initial:
                conf_alloc_ratio = getattr(CONF, 'initial_%s' % attr)
            else:
                conf_alloc_ratio = getattr(self, attr)
            if conf_alloc_ratio not in (0.0, None):
                setattr(compute_node, attr, conf_alloc_ratio)
        compute_node.update_from_virt_driver(resources)

    def remove_node(self, nodename):
        """Handle node removal/rebalance.

        Clean up any stored data about a compute node no longer
        managed by this host.
        """
        self.stats.pop(nodename, None)
        self.compute_nodes.pop(nodename, None)
        self.old_resources.pop(nodename, None)

    def _get_host_metrics(self, context, nodename):
        """Get the metrics from monitors and
        notify information to message bus.
        """
        metrics = objects.MonitorMetricList()
        metrics_info = {}
        for monitor in self.monitors:
            try:
                monitor.populate_metrics(metrics)
            except NotImplementedError:
                LOG.debug("The compute driver doesn't support host metrics for  %(mon)s", {'mon': monitor})
            except Exception as exc:
                LOG.warning('Cannot get the metrics from %(mon)s; error: %(exc)s', {'mon': monitor, 'exc': exc})
        metric_list = metrics.to_list()
        if len(metric_list):
            metrics_info['nodename'] = nodename
            metrics_info['metrics'] = metric_list
            metrics_info['host'] = self.host
            metrics_info['host_ip'] = CONF.my_ip
            notifier = rpc.get_notifier(service='compute', host=nodename)
            notifier.info(context, 'compute.metrics.update', metrics_info)
            compute_utils.notify_about_metrics_update(context, self.host, CONF.my_ip, nodename, metrics)
        return metric_list

    def update_available_resource(self, context, nodename, startup=False):
        """Override in-memory calculations of compute node resource usage based
        on data audited from the hypervisor layer.

        Add in resource claims in progress to account for operations that have
        declared a need for resources, but not necessarily retrieved them from
        the hypervisor layer yet.

        :param nodename: Temporary parameter representing the Ironic resource
                         node. This parameter will be removed once Ironic
                         baremetal resource nodes are handled like any other
                         resource in the system.
        :param startup: Boolean indicating whether we're running this on
                        on startup (True) or periodic (False).
        """
        LOG.debug('Auditing locally available compute resources for %(host)s (node: %(node)s)', {'node': nodename, 'host': self.host})
        resources = self.driver.get_available_resource(nodename)
        resources['host_ip'] = CONF.my_ip
        if 'cpu_info' not in resources or resources['cpu_info'] is None:
            resources['cpu_info'] = ''
        self._verify_resources(resources)
        self._report_hypervisor_resource_view(resources)
        self._update_available_resource(context, resources, startup=startup)

    def _pair_instances_to_migrations(self, migrations, instance_by_uuid):
        for migration in migrations:
            try:
                migration.instance = instance_by_uuid[migration.instance_uuid]
            except KeyError:
                LOG.debug("Migration for instance %(uuid)s refers to another host's instance!", {'uuid': migration.instance_uuid})

    @utils.synchronized(COMPUTE_RESOURCE_SEMAPHORE, fair=True)
    def _update_available_resource(self, context, resources, startup=False):
        is_new_compute_node = self._init_compute_node(context, resources)
        nodename = resources['hypervisor_hostname']
        if self.disabled(nodename):
            return
        instances = objects.InstanceList.get_by_host_and_node(context, self.host, nodename, expected_attrs=['system_metadata', 'numa_topology', 'flavor', 'migration_context', 'resources'])
        instance_by_uuid = self._update_usage_from_instances(context, instances, nodename)
        migrations = objects.MigrationList.get_in_progress_and_error(context, self.host, nodename)
        self._pair_instances_to_migrations(migrations, instance_by_uuid)
        self._update_usage_from_migrations(context, migrations, nodename)
        if not is_new_compute_node:
            self._remove_deleted_instances_allocations(context, self.compute_nodes[nodename], migrations, instance_by_uuid)
        cn = self.compute_nodes[nodename]
        self.pci_tracker.clean_usage(instances, migrations)
        dev_pools_obj = self.pci_tracker.stats.to_device_pools_obj()
        cn.pci_device_pools = dev_pools_obj
        self._report_final_resource_view(nodename)
        metrics = self._get_host_metrics(context, nodename)
        cn.metrics = jsonutils.dumps(metrics)
        self._populate_assigned_resources(context, instance_by_uuid)
        self._update(context, cn, startup=startup)
        LOG.debug('Compute_service record updated for %(host)s:%(node)s', {'host': self.host, 'node': nodename})
        if startup:
            self._check_resources(context)

    def _get_compute_node(self, context, nodename):
        """Returns compute node for the host and nodename."""
        try:
            return objects.ComputeNode.get_by_host_and_nodename(context, self.host, nodename)
        except exception.NotFound:
            LOG.warning('No compute node record for %(host)s:%(node)s', {'host': self.host, 'node': nodename})

    def _report_hypervisor_resource_view(self, resources):
        """Log the hypervisor's view of free resources.

        This is just a snapshot of resource usage recorded by the
        virt driver.

        The following resources are logged:
            - free memory
            - free disk
            - free CPUs
            - assignable PCI devices
        """
        nodename = resources['hypervisor_hostname']
        free_ram_mb = resources['memory_mb'] - resources['memory_mb_used']
        free_disk_gb = resources['local_gb'] - resources['local_gb_used']
        vcpus = resources['vcpus']
        if vcpus:
            free_vcpus = vcpus - resources['vcpus_used']
        else:
            free_vcpus = 'unknown'
        pci_devices = resources.get('pci_passthrough_devices')
        LOG.debug('Hypervisor/Node resource view: name=%(node)s free_ram=%(free_ram)sMB free_disk=%(free_disk)sGB free_vcpus=%(free_vcpus)s pci_devices=%(pci_devices)s', {'node': nodename, 'free_ram': free_ram_mb, 'free_disk': free_disk_gb, 'free_vcpus': free_vcpus, 'pci_devices': pci_devices})

    def _report_final_resource_view(self, nodename):
        """Report final calculate of physical memory, used virtual memory,
        disk, usable vCPUs, used virtual CPUs and PCI devices,
        including instance calculations and in-progress resource claims. These
        values will be exposed via the compute node table to the scheduler.
        """
        cn = self.compute_nodes[nodename]
        vcpus = cn.vcpus
        if vcpus:
            tcpu = vcpus
            ucpu = cn.vcpus_used
            LOG.debug('Total usable vcpus: %(tcpu)s, total allocated vcpus: %(ucpu)s', {'tcpu': vcpus, 'ucpu': ucpu})
        else:
            tcpu = 0
            ucpu = 0
        pci_stats = list(cn.pci_device_pools) if cn.pci_device_pools else []
        LOG.debug('Final resource view: name=%(node)s phys_ram=%(phys_ram)sMB used_ram=%(used_ram)sMB phys_disk=%(phys_disk)sGB used_disk=%(used_disk)sGB total_vcpus=%(total_vcpus)s used_vcpus=%(used_vcpus)s pci_stats=%(pci_stats)s', {'node': nodename, 'phys_ram': cn.memory_mb, 'used_ram': cn.memory_mb_used, 'phys_disk': cn.local_gb, 'used_disk': cn.local_gb_used, 'total_vcpus': tcpu, 'used_vcpus': ucpu, 'pci_stats': pci_stats})

    def _resource_change(self, compute_node):
        """Check to see if any resources have changed."""
        nodename = compute_node.hypervisor_hostname
        old_compute = self.old_resources[nodename]
        if not obj_base.obj_equal_prims(compute_node, old_compute, ['updated_at']):
            self.old_resources[nodename] = copy.deepcopy(compute_node)
            return True
        return False

    def _sync_compute_service_disabled_trait(self, context, traits):
        """Synchronize the COMPUTE_STATUS_DISABLED trait on the node provider.

        Determines if the COMPUTE_STATUS_DISABLED trait should be added to
        or removed from the provider's set of traits based on the related
        nova-compute service disabled status.

        :param context: RequestContext for cell database access
        :param traits: set of traits for the compute node resource provider;
            this is modified by reference
        """
        trait = os_traits.COMPUTE_STATUS_DISABLED
        try:
            service = objects.Service.get_by_compute_host(context, self.host)
            if service.disabled:
                traits.add(trait)
            else:
                traits.discard(trait)
        except exception.NotFound:
            LOG.error('Unable to find services table record for nova-compute host %s', self.host)

    def _get_traits(self, context, nodename, provider_tree):
        """Synchronizes internal and external traits for the node provider.

        This works in conjunction with the ComptueDriver.update_provider_tree
        flow and is used to synchronize traits reported by the compute driver,
        traits based on information in the ComputeNode record, and traits set
        externally using the placement REST API.

        :param context: RequestContext for cell database access
        :param nodename: ComputeNode.hypervisor_hostname for the compute node
            resource provider whose traits are being synchronized; the node
            must be in the ProviderTree.
        :param provider_tree: ProviderTree being updated
        """
        traits = provider_tree.data(nodename).traits
        for (trait, supported) in self.driver.capabilities_as_traits().items():
            if supported:
                traits.add(trait)
            elif trait in traits:
                traits.remove(trait)
        traits.add(os_traits.COMPUTE_NODE)
        self._sync_compute_service_disabled_trait(context, traits)
        return list(traits)

    @retrying.retry(stop_max_attempt_number=4, retry_on_exception=lambda e: isinstance(e, exception.ResourceProviderUpdateConflict))
    def _update_to_placement(self, context, compute_node, startup):
        """Send resource and inventory changes to placement."""
        nodename = compute_node.hypervisor_hostname
        prov_tree = self.reportclient.get_provider_tree_and_ensure_root(context, compute_node.uuid, name=compute_node.hypervisor_hostname)
        allocs = None
        try:
            self.driver.update_provider_tree(prov_tree, nodename)
        except exception.ReshapeNeeded:
            if not startup:
                raise
            LOG.info('Performing resource provider inventory and allocation data migration during compute service startup or fast-forward upgrade.')
            allocs = self.reportclient.get_allocations_for_provider_tree(context, nodename)
            self.driver.update_provider_tree(prov_tree, nodename, allocations=allocs)
        traits = self._get_traits(context, nodename, provider_tree=prov_tree)
        prov_tree.update_traits(nodename, traits)
        self.provider_tree = prov_tree
        self._merge_provider_configs(self.provider_configs, prov_tree)
        self.reportclient.update_from_provider_tree(context, prov_tree, allocations=allocs)

    def _update(self, context, compute_node, startup=False):
        """Update partial stats locally and populate them to Scheduler."""
        nodename = compute_node.hypervisor_hostname
        old_compute = self.old_resources[nodename]
        if self._resource_change(compute_node):
            try:
                compute_node.save()
            except Exception:
                with excutils.save_and_reraise_exception(logger=LOG):
                    self.old_resources[nodename] = old_compute
        self._update_to_placement(context, compute_node, startup)
        if self.pci_tracker:
            self.pci_tracker.save(context)

    def _update_usage(self, usage, nodename, sign=1):
        mem_usage = usage['memory_mb']
        disk_usage = usage.get('root_gb', 0)
        vcpus_usage = usage.get('vcpus', 0)
        cn = self.compute_nodes[nodename]
        cn.memory_mb_used += sign * mem_usage
        cn.local_gb_used += sign * disk_usage
        cn.local_gb_used += sign * usage.get('ephemeral_gb', 0)
        cn.local_gb_used += sign * usage.get('swap', 0) / 1024
        cn.vcpus_used += sign * vcpus_usage
        cn.free_ram_mb = cn.memory_mb - cn.memory_mb_used
        cn.free_disk_gb = cn.local_gb - cn.local_gb_used
        stats = self.stats[nodename]
        cn.running_vms = stats.num_instances
        if cn.numa_topology and usage.get('numa_topology'):
            instance_numa_topology = usage.get('numa_topology')
            host_numa_topology = objects.NUMATopology.obj_from_db_obj(cn.numa_topology)
            free = sign == -1
            cn.numa_topology = hardware.numa_usage_from_instance_numa(host_numa_topology, instance_numa_topology, free)._to_json()

    def _get_migration_context_resource(self, resource, instance, prefix='new_'):
        migration_context = instance.migration_context
        resource = prefix + resource
        if migration_context and resource in migration_context:
            return getattr(migration_context, resource)
        return None

    def _update_usage_from_migration(self, context, instance, migration, nodename):
        """Update usage for a single migration.  The record may
        represent an incoming or outbound migration.
        """
        uuid = migration.instance_uuid
        LOG.info('Updating resource usage from migration %s', migration.uuid, instance_uuid=uuid)
        incoming = migration.dest_compute == self.host and migration.dest_node == nodename
        outbound = migration.source_compute == self.host and migration.source_node == nodename
        same_node = incoming and outbound
        tracked = uuid in self.tracked_instances
        itype = None
        numa_topology = None
        sign = 0
        if same_node:
            if instance['instance_type_id'] == migration.old_instance_type_id:
                itype = self._get_instance_type(instance, 'new_', migration)
                numa_topology = self._get_migration_context_resource('numa_topology', instance)
                sign = 1
            else:
                itype = self._get_instance_type(instance, 'old_', migration)
                numa_topology = self._get_migration_context_resource('numa_topology', instance, prefix='old_')
        elif incoming and (not tracked):
            itype = self._get_instance_type(instance, 'new_', migration)
            numa_topology = self._get_migration_context_resource('numa_topology', instance)
            sign = 1
            LOG.debug('Starting to track incoming migration %s with flavor %s', migration.uuid, itype.flavorid, instance=instance)
        elif outbound and (not tracked):
            itype = self._get_instance_type(instance, 'old_', migration)
            numa_topology = self._get_migration_context_resource('numa_topology', instance, prefix='old_')
            if itype:
                LOG.debug('Starting to track outgoing migration %s with flavor %s', migration.uuid, itype.flavorid, instance=instance)
        if itype:
            cn = self.compute_nodes[nodename]
            usage = self._get_usage_dict(itype, instance, numa_topology=numa_topology)
            if self.pci_tracker and sign:
                self.pci_tracker.update_pci_for_instance(context, instance, sign=sign)
            self._update_usage(usage, nodename)
            if self.pci_tracker:
                obj = self.pci_tracker.stats.to_device_pools_obj()
                cn.pci_device_pools = obj
            else:
                obj = objects.PciDevicePoolList()
                cn.pci_device_pools = obj
            self.tracked_migrations[uuid] = migration

    def _update_usage_from_migrations(self, context, migrations, nodename):
        filtered = {}
        instances = {}
        self.tracked_migrations.clear()
        for migration in migrations:
            uuid = migration.instance_uuid
            try:
                if uuid not in instances:
                    migration._context = context.elevated(read_deleted='yes')
                    instances[uuid] = migration.instance
            except exception.InstanceNotFound as e:
                LOG.debug('Migration instance not found: %s', e)
                continue
            if not _instance_in_resize_state(instances[uuid]) and (not _instance_is_live_migrating(instances[uuid])):
                LOG.debug('Skipping migration as instance is neither resizing nor live-migrating.', instance_uuid=uuid)
                continue
            other_migration = filtered.get(uuid, None)
            if other_migration:
                om = other_migration
                other_time = om.updated_at or om.created_at
                migration_time = migration.updated_at or migration.created_at
                if migration_time > other_time:
                    filtered[uuid] = migration
            else:
                filtered[uuid] = migration
        for migration in filtered.values():
            instance = instances[migration.instance_uuid]
            if instance.migration_context is not None and instance.migration_context.migration_id != migration.id:
                LOG.info("Current instance migration %(im)s doesn't match migration %(m)s, marking migration as error. This can occur if a previous migration for this instance did not complete.", {'im': instance.migration_context.migration_id, 'm': migration.id})
                migration.status = 'error'
                migration.save()
                continue
            try:
                self._update_usage_from_migration(context, instance, migration, nodename)
            except exception.FlavorNotFound:
                LOG.warning('Flavor could not be found, skipping migration.', instance_uuid=instance.uuid)
                continue

    def _update_usage_from_instance(self, context, instance, nodename, is_removed=False):
        """Update usage for a single instance."""
        uuid = instance['uuid']
        is_new_instance = uuid not in self.tracked_instances
        is_removed_instance = not is_new_instance and (is_removed or instance['vm_state'] in vm_states.ALLOW_RESOURCE_REMOVAL)
        if is_new_instance:
            self.tracked_instances.add(uuid)
            sign = 1
        if is_removed_instance:
            self.tracked_instances.remove(uuid)
            self._release_assigned_resources(instance.resources)
            sign = -1
        cn = self.compute_nodes[nodename]
        stats = self.stats[nodename]
        stats.update_stats_for_instance(instance, is_removed_instance)
        cn.stats = stats
        if is_new_instance or is_removed_instance:
            if self.pci_tracker:
                self.pci_tracker.update_pci_for_instance(context, instance, sign=sign)
            self._update_usage(self._get_usage_dict(instance, instance), nodename, sign=sign)
        if is_removed_instance and uuid in self.is_bfv:
            del self.is_bfv[uuid]
        cn.current_workload = stats.calculate_workload()
        if self.pci_tracker:
            obj = self.pci_tracker.stats.to_device_pools_obj()
            cn.pci_device_pools = obj
        else:
            cn.pci_device_pools = objects.PciDevicePoolList()

    def _update_usage_from_instances(self, context, instances, nodename):
        """Calculate resource usage based on instance utilization.  This is
        different than the hypervisor's view as it will account for all
        instances assigned to the local compute host, even if they are not
        currently powered on.
        """
        self.tracked_instances.clear()
        cn = self.compute_nodes[nodename]
        cn.local_gb_used = CONF.reserved_host_disk_mb / 1024
        cn.memory_mb_used = CONF.reserved_host_memory_mb
        cn.vcpus_used = CONF.reserved_host_cpus
        cn.free_ram_mb = cn.memory_mb - cn.memory_mb_used
        cn.free_disk_gb = cn.local_gb - cn.local_gb_used
        cn.current_workload = 0
        cn.running_vms = 0
        instance_by_uuid = {}
        for instance in instances:
            if instance.vm_state not in vm_states.ALLOW_RESOURCE_REMOVAL:
                self._update_usage_from_instance(context, instance, nodename)
            instance_by_uuid[instance.uuid] = instance
        return instance_by_uuid

    def _remove_deleted_instances_allocations(self, context, cn, migrations, instance_by_uuid):
        migration_uuids = [migration.uuid for migration in migrations if 'uuid' in migration]
        try:
            pai = self.reportclient.get_allocations_for_resource_provider(context, cn.uuid)
        except (exception.ResourceProviderAllocationRetrievalFailed, ks_exc.ClientException) as e:
            LOG.error('Skipping removal of allocations for deleted instances: %s', e)
            return
        allocations = pai.allocations
        if not allocations:
            return
        read_deleted_context = context.elevated(read_deleted='yes')
        for (consumer_uuid, alloc) in allocations.items():
            if consumer_uuid in self.tracked_instances:
                LOG.debug('Instance %s actively managed on this compute host and has allocations in placement: %s.', consumer_uuid, alloc)
                continue
            if consumer_uuid in migration_uuids:
                LOG.debug('Migration %s is active on this compute host and has allocations in placement: %s.', consumer_uuid, alloc)
                continue
            instance_uuid = consumer_uuid
            instance = instance_by_uuid.get(instance_uuid)
            if not instance:
                try:
                    instance = objects.Instance.get_by_uuid(read_deleted_context, consumer_uuid, expected_attrs=[])
                except exception.InstanceNotFound:
                    LOG.info('Instance %(uuid)s has allocations against this compute host but is not found in the database.', {'uuid': instance_uuid}, exc_info=False)
                    continue
            if instance.deleted and (not instance.hidden):
                LOG.debug('Instance %s has been deleted (perhaps locally). Deleting allocations that remained for this instance against this compute host: %s.', instance_uuid, alloc)
                self.reportclient.delete_allocation_for_instance(context, instance_uuid)
                continue
            if not instance.host:
                LOG.debug('Instance %s has been scheduled to this compute host, the scheduler has made an allocation against this compute node but the instance has yet to start. Skipping heal of allocation: %s.', instance_uuid, alloc)
                continue
            if instance.host == cn.host and instance.node == cn.hypervisor_hostname:
                if instance.task_state:
                    LOG.debug('Instance with task_state "%s" is not being actively managed by this compute host but has allocations referencing this compute node (%s): %s. Skipping heal of allocations during the task state transition.', instance.task_state, cn.uuid, alloc, instance=instance)
                else:
                    LOG.warning('Instance %s is not being actively managed by this compute host but has allocations referencing this compute host: %s. Skipping heal of allocation because we do not know what to do.', instance_uuid, alloc)
                continue
            if instance.host != cn.host:
                LOG.warning('Instance %s has been moved to another host %s(%s). There are allocations remaining against the source host that might need to be removed: %s.', instance_uuid, instance.host, instance.node, alloc)

    def delete_allocation_for_evacuated_instance(self, context, instance, node, node_type='source'):
        cn_uuid = self.compute_nodes[node].uuid
        if not self.reportclient.remove_provider_tree_from_instance_allocation(context, instance.uuid, cn_uuid):
            LOG.error('Failed to clean allocation of evacuated instance on the %s node %s', node_type, cn_uuid, instance=instance)

    def delete_allocation_for_shelve_offloaded_instance(self, context, instance):
        self.reportclient.delete_allocation_for_instance(context, instance.uuid)

    def _verify_resources(self, resources):
        resource_keys = ['vcpus', 'memory_mb', 'local_gb', 'cpu_info', 'vcpus_used', 'memory_mb_used', 'local_gb_used', 'numa_topology']
        missing_keys = [k for k in resource_keys if k not in resources]
        if missing_keys:
            reason = _('Missing keys: %s') % missing_keys
            raise exception.InvalidInput(reason=reason)

    def _get_instance_type(self, instance, prefix, migration):
        """Get the instance type from instance."""
        if migration.is_resize:
            return getattr(instance, '%sflavor' % prefix)
        else:
            return instance.flavor

    def _get_usage_dict(self, object_or_dict, instance, **updates):
        """Make a usage dict _update methods expect.

        Accepts a dict or an Instance or Flavor object, and a set of updates.
        Converts the object to a dict and applies the updates.

        :param object_or_dict: instance or flavor as an object or just a dict
        :param instance: nova.objects.Instance for the related operation; this
                         is needed to determine if the instance is
                         volume-backed
        :param updates: key-value pairs to update the passed object.
                        Currently only considers 'numa_topology', all other
                        keys are ignored.

        :returns: a dict with all the information from object_or_dict updated
                  with updates
        """

        def _is_bfv():
            if instance.uuid in self.is_bfv:
                is_bfv = self.is_bfv[instance.uuid]
            else:
                is_bfv = compute_utils.is_volume_backed_instance(instance._context, instance)
                self.is_bfv[instance.uuid] = is_bfv
            return is_bfv
        usage = {}
        if isinstance(object_or_dict, objects.Instance):
            is_bfv = _is_bfv()
            usage = {'memory_mb': object_or_dict.flavor.memory_mb, 'swap': object_or_dict.flavor.swap, 'vcpus': object_or_dict.flavor.vcpus, 'root_gb': 0 if is_bfv else object_or_dict.flavor.root_gb, 'ephemeral_gb': object_or_dict.flavor.ephemeral_gb, 'numa_topology': object_or_dict.numa_topology}
        elif isinstance(object_or_dict, objects.Flavor):
            usage = obj_base.obj_to_primitive(object_or_dict)
            if _is_bfv():
                usage['root_gb'] = 0
        else:
            usage.update(object_or_dict)
        for key in ('numa_topology',):
            if key in updates:
                usage[key] = updates[key]
        return usage

    def _merge_provider_configs(self, provider_configs, provider_tree):
        """Takes a provider tree and merges any provider configs. Any
        providers in the update that are not present in the tree are logged
        and ignored. Providers identified by both $COMPUTE_NODE and explicit
        UUID/NAME will only be updated with the additional inventories and
        traits in the explicit provider config entry.

        :param provider_configs: The provider configs to merge
        :param provider_tree: The provider tree to be updated in place
        """
        processed_providers = {}
        provider_custom_traits = {}
        for (uuid_or_name, provider_data) in provider_configs.items():
            additional_traits = provider_data.get('traits', {}).get('additional', [])
            additional_inventories = provider_data.get('inventories', {}).get('additional', [])
            source_file_name = provider_data['__source_file']
            providers_to_update = self._get_providers_to_update(uuid_or_name, provider_tree, source_file_name)
            for provider in providers_to_update:
                if uuid_or_name == '$COMPUTE_NODE':
                    if any((_pid in provider_configs for _pid in [provider.name, provider.uuid])):
                        continue
                current_uuid = provider.uuid
                if current_uuid in processed_providers:
                    raise ValueError(_("Provider config '%(source_file_name)s' conflicts with provider config '%(processed_providers)s'. The same provider is specified using both name '%(uuid_or_name)s' and uuid '%(current_uuid)s'.") % {'source_file_name': source_file_name, 'processed_providers': processed_providers[current_uuid], 'uuid_or_name': uuid_or_name, 'current_uuid': current_uuid})
                if current_uuid not in provider_custom_traits:
                    provider_custom_traits[current_uuid] = {trait for trait in provider.traits if trait.startswith('CUSTOM')}
                existing_custom_traits = provider_custom_traits[current_uuid]
                if additional_traits:
                    intersect = set(provider.traits) & set(additional_traits)
                    intersect -= existing_custom_traits
                    if intersect:
                        invalid = ','.join(intersect)
                        raise ValueError(_("Provider config '%(source_file_name)s' attempts to define a trait that is owned by the virt driver or specified via the placment api. Invalid traits '%(invalid)s' must be removed from '%(source_file_name)s'.") % {'source_file_name': source_file_name, 'invalid': invalid})
                    provider_tree.add_traits(provider.uuid, *additional_traits)
                if additional_inventories:
                    merged_inventory = provider.inventory
                    intersect = merged_inventory.keys() & {rc for inv_dict in additional_inventories for rc in inv_dict}
                    if intersect:
                        raise ValueError(_("Provider config '%(source_file_name)s' attempts to define an inventory that is owned by the virt driver. Invalid inventories '%(invalid)s' must be removed from '%(source_file_name)s'.") % {'source_file_name': source_file_name, 'invalid': ','.join(intersect)})
                    for inventory in additional_inventories:
                        merged_inventory.update(inventory)
                    provider_tree.update_inventory(provider.uuid, merged_inventory)
                processed_providers[current_uuid] = source_file_name

    def _get_providers_to_update(self, uuid_or_name, provider_tree, source_file):
        """Identifies the providers to be updated.
        Intended only to be consumed by _merge_provider_configs()

        :param provider: Provider config data
        :param provider_tree: Provider tree to get providers from
        :param source_file: Provider config file containing the inventories

        :returns: list of ProviderData
        """
        if uuid_or_name == '$COMPUTE_NODE':
            return [root.data() for root in provider_tree.roots if os_traits.COMPUTE_NODE in root.traits]
        try:
            providers_to_update = [provider_tree.data(uuid_or_name)]
            self.absent_providers.discard(uuid_or_name)
        except ValueError:
            providers_to_update = []
            if uuid_or_name not in self.absent_providers:
                LOG.warning("Provider '%(uuid_or_name)s' specified in provider config file '%(source_file)s' does not exist in the ProviderTree and will be ignored.", {'uuid_or_name': uuid_or_name, 'source_file': source_file})
                self.absent_providers.add(uuid_or_name)
        return providers_to_update

    def build_failed(self, nodename):
        """Increments the failed_builds stats for the given node."""
        self.stats[nodename].build_failed()

    def build_succeeded(self, nodename):
        """Resets the failed_builds stats for the given node."""
        self.stats[nodename].build_succeeded()

    @utils.synchronized(COMPUTE_RESOURCE_SEMAPHORE, fair=True)
    def claim_pci_devices(self, context, pci_requests, instance_numa_topology):
        """Claim instance PCI resources

        :param context: security context
        :param pci_requests: a list of nova.objects.InstancePCIRequests
        :param instance_numa_topology: an InstanceNumaTopology object used to
            ensure PCI devices are aligned with the NUMA topology of the
            instance
        :returns: a list of nova.objects.PciDevice objects
        """
        result = self.pci_tracker.claim_instance(context, pci_requests, instance_numa_topology)
        self.pci_tracker.save(context)
        return result

    @utils.synchronized(COMPUTE_RESOURCE_SEMAPHORE, fair=True)
    def unclaim_pci_devices(self, context, pci_device, instance):
        """Deallocate PCI devices

        :param context: security context
        :param pci_device: the objects.PciDevice describing the PCI device to
            be freed
        :param instance: the objects.Instance the PCI resources are freed from
        """
        self.pci_tracker.free_device(pci_device, instance)
        self.pci_tracker.save(context)

    @utils.synchronized(COMPUTE_RESOURCE_SEMAPHORE, fair=True)
    def allocate_pci_devices_for_instance(self, context, instance):
        """Allocate instance claimed PCI resources

        :param context: security context
        :param instance: instance object
        """
        self.pci_tracker.allocate_instance(instance)
        self.pci_tracker.save(context)

    @utils.synchronized(COMPUTE_RESOURCE_SEMAPHORE, fair=True)
    def free_pci_device_allocations_for_instance(self, context, instance):
        """Free instance allocated PCI resources

        :param context: security context
        :param instance: instance object
        """
        self.pci_tracker.free_instance_allocations(context, instance)
        self.pci_tracker.save(context)

    @utils.synchronized(COMPUTE_RESOURCE_SEMAPHORE, fair=True)
    def free_pci_device_claims_for_instance(self, context, instance):
        """Free instance claimed PCI resources

        :param context: security context
        :param instance: instance object
        """
        self.pci_tracker.free_instance_claims(context, instance)
        self.pci_tracker.save(context)

    @utils.synchronized(COMPUTE_RESOURCE_SEMAPHORE, fair=True)
    def finish_evacuation(self, instance, node, migration):
        instance.apply_migration_context()
        instance.host = self.host
        instance.node = node
        instance.save()
        instance.drop_migration_context()
        if migration:
            migration.status = 'done'
            migration.save()