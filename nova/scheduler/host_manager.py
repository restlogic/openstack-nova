from bees import profiler as p
'\nManage hosts in the current zone.\n'
import collections
import functools
import time
try:
    from collections import UserDict as IterableUserDict
except ImportError:
    from UserDict import IterableUserDict
import iso8601
from oslo_log import log as logging
from oslo_utils import timeutils
import nova.conf
from nova import context as context_module
from nova import exception
from nova import objects
from nova.pci import stats as pci_stats
from nova.scheduler import filters
from nova.scheduler import weights
from nova import utils
from nova.virt import hardware
CONF = nova.conf.CONF
LOG = logging.getLogger(__name__)
HOST_INSTANCE_SEMAPHORE = 'host_instance'

@p.trace_cls('ReadOnlyDict')
class ReadOnlyDict(IterableUserDict):
    """A read-only dict."""

    def __init__(self, source=None):
        self.data = {}
        if source:
            self.data.update(source)

    def __setitem__(self, key, item):
        raise TypeError()

    def __delitem__(self, key):
        raise TypeError()

    def clear(self):
        raise TypeError()

    def pop(self, key, *args):
        raise TypeError()

    def popitem(self):
        raise TypeError()

    def update(self):
        raise TypeError()

@p.trace('set_update_time_on_success')
@utils.expects_func_args('self', 'spec_obj')
def set_update_time_on_success(function):
    """Set updated time of HostState when consuming succeed."""

    @functools.wraps(function)
    def decorated_function(self, spec_obj):
        return_value = None
        try:
            return_value = function(self, spec_obj)
        except Exception as e:
            LOG.warning('Selected host: %(host)s failed to consume from instance. Error: %(error)s', {'host': self.host, 'error': e})
        else:
            now = timeutils.utcnow()
            self.updated = now.replace(tzinfo=iso8601.UTC)
        return return_value
    return decorated_function

@p.trace_cls('HostState')
class HostState(object):
    """Mutable and immutable information tracked for a host.
    This is an attempt to remove the ad-hoc data structures
    previously used and lock down access.
    """

    def __init__(self, host, node, cell_uuid):
        self.host = host
        self.nodename = node
        self.uuid = None
        self._lock_name = (host, node)
        self.total_usable_ram_mb = 0
        self.total_usable_disk_gb = 0
        self.disk_mb_used = 0
        self.free_ram_mb = 0
        self.free_disk_mb = 0
        self.vcpus_total = 0
        self.vcpus_used = 0
        self.pci_stats = None
        self.numa_topology = None
        self.num_instances = 0
        self.num_io_ops = 0
        self.failed_builds = 0
        self.host_ip = None
        self.hypervisor_type = None
        self.hypervisor_version = None
        self.hypervisor_hostname = None
        self.cpu_info = None
        self.supported_instances = None
        self.limits = {}
        self.metrics = None
        self.aggregates = []
        self.instances = {}
        self.ram_allocation_ratio = None
        self.cpu_allocation_ratio = None
        self.disk_allocation_ratio = None
        self.cell_uuid = cell_uuid
        self.updated = None

    def update(self, compute=None, service=None, aggregates=None, inst_dict=None):
        """Update all information about a host."""

        @utils.synchronized(self._lock_name)
        def _locked_update(self, compute, service, aggregates, inst_dict):
            if compute is not None:
                LOG.debug('Update host state from compute node: %s', compute)
                self._update_from_compute_node(compute)
            if aggregates is not None:
                LOG.debug('Update host state with aggregates: %s', aggregates)
                self.aggregates = aggregates
            if service is not None:
                LOG.debug('Update host state with service dict: %s', service)
                self.service = ReadOnlyDict(service)
            if inst_dict is not None:
                LOG.debug('Update host state with instances: %s', list(inst_dict))
                self.instances = inst_dict
        return _locked_update(self, compute, service, aggregates, inst_dict)

    def _update_from_compute_node(self, compute):
        """Update information about a host from a ComputeNode object."""
        if 'free_disk_gb' not in compute or compute.free_disk_gb is None:
            LOG.debug('Ignoring compute node %s as its usage has not been updated yet.', compute.uuid)
            return
        if self.updated and compute.updated_at and (self.updated > compute.updated_at):
            return
        all_ram_mb = compute.memory_mb
        self.uuid = compute.uuid
        free_gb = compute.free_disk_gb
        least_gb = compute.disk_available_least
        if least_gb is not None:
            if least_gb > free_gb:
                LOG.warning('Host %(hostname)s has more disk space than database expected (%(physical)s GB > %(database)s GB)', {'physical': least_gb, 'database': free_gb, 'hostname': compute.hypervisor_hostname})
            free_gb = min(least_gb, free_gb)
        free_disk_mb = free_gb * 1024
        self.disk_mb_used = compute.local_gb_used * 1024
        self.free_ram_mb = compute.free_ram_mb
        self.total_usable_ram_mb = all_ram_mb
        self.total_usable_disk_gb = compute.local_gb
        self.free_disk_mb = free_disk_mb
        self.vcpus_total = compute.vcpus
        self.vcpus_used = compute.vcpus_used
        self.updated = compute.updated_at
        self.numa_topology = objects.NUMATopology.obj_from_db_obj(compute.numa_topology) if compute.numa_topology else None
        self.pci_stats = pci_stats.PciDeviceStats(self.numa_topology, stats=compute.pci_device_pools)
        self.host_ip = compute.host_ip
        self.hypervisor_type = compute.hypervisor_type
        self.hypervisor_version = compute.hypervisor_version
        self.hypervisor_hostname = compute.hypervisor_hostname
        self.cpu_info = compute.cpu_info
        if compute.supported_hv_specs:
            self.supported_instances = [spec.to_list() for spec in compute.supported_hv_specs]
        else:
            self.supported_instances = []
        self.stats = compute.stats or {}
        self.num_instances = int(self.stats.get('num_instances', 0))
        self.num_io_ops = int(self.stats.get('io_workload', 0))
        self.metrics = objects.MonitorMetricList.from_json(compute.metrics)
        self.cpu_allocation_ratio = compute.cpu_allocation_ratio
        self.ram_allocation_ratio = compute.ram_allocation_ratio
        self.disk_allocation_ratio = compute.disk_allocation_ratio
        self.failed_builds = int(self.stats.get('failed_builds', 0))

    def consume_from_request(self, spec_obj):
        """Incrementally update host state from a RequestSpec object."""

        @utils.synchronized(self._lock_name)
        @set_update_time_on_success
        def _locked(self, spec_obj):
            self._locked_consume_from_request(spec_obj)
        return _locked(self, spec_obj)

    def _locked_consume_from_request(self, spec_obj):
        disk_mb = (spec_obj.root_gb + spec_obj.ephemeral_gb) * 1024
        ram_mb = spec_obj.memory_mb
        vcpus = spec_obj.vcpus
        self.free_ram_mb -= ram_mb
        self.free_disk_mb -= disk_mb
        self.vcpus_used += vcpus
        self.num_instances += 1
        pci_requests = spec_obj.pci_requests
        if pci_requests and self.pci_stats:
            pci_requests = pci_requests.requests
        else:
            pci_requests = None
        if self.numa_topology and spec_obj.numa_topology:
            spec_obj.numa_topology = hardware.numa_fit_instance_to_host(self.numa_topology, spec_obj.numa_topology, limits=self.limits.get('numa_topology'), pci_requests=pci_requests, pci_stats=self.pci_stats)
            self.numa_topology = hardware.numa_usage_from_instance_numa(self.numa_topology, spec_obj.numa_topology)
        if pci_requests:
            instance_cells = None
            if spec_obj.numa_topology:
                instance_cells = spec_obj.numa_topology.cells
            self.pci_stats.apply_requests(pci_requests, instance_cells)
        self.num_io_ops += 1

    def __repr__(self):
        return '(%(host)s, %(node)s) ram: %(free_ram)sMB disk: %(free_disk)sMB io_ops: %(num_io_ops)s instances: %(num_instances)s' % {'host': self.host, 'node': self.nodename, 'free_ram': self.free_ram_mb, 'free_disk': self.free_disk_mb, 'num_io_ops': self.num_io_ops, 'num_instances': self.num_instances}

@p.trace_cls('HostManager')
class HostManager(object):
    """Base HostManager class."""

    def host_state_cls(self, host, node, cell, **kwargs):
        return HostState(host, node, cell)

    def __init__(self):
        self.refresh_cells_caches()
        self.filter_handler = filters.HostFilterHandler()
        filter_classes = self.filter_handler.get_matching_classes(CONF.filter_scheduler.available_filters)
        self.filter_cls_map = {cls.__name__: cls for cls in filter_classes}
        self.filter_obj_map = {}
        self.enabled_filters = self._choose_host_filters(self._load_filters())
        self.weight_handler = weights.HostWeightHandler()
        weigher_classes = self.weight_handler.get_matching_classes(CONF.filter_scheduler.weight_classes)
        self.weighers = [cls() for cls in weigher_classes]
        self.aggs_by_id = {}
        self.host_aggregates_map = collections.defaultdict(set)
        self._init_aggregates()
        self.track_instance_changes = CONF.filter_scheduler.track_instance_changes
        self._instance_info = {}
        if self.track_instance_changes:
            self._init_instance_info()

    def _load_filters(self):
        return CONF.filter_scheduler.enabled_filters

    def _init_aggregates(self):
        elevated = context_module.get_admin_context()
        aggs = objects.AggregateList.get_all(elevated)
        for agg in aggs:
            self.aggs_by_id[agg.id] = agg
            for host in agg.hosts:
                self.host_aggregates_map[host].add(agg.id)

    def update_aggregates(self, aggregates):
        """Updates internal HostManager information about aggregates."""
        if isinstance(aggregates, (list, objects.AggregateList)):
            for agg in aggregates:
                self._update_aggregate(agg)
        else:
            self._update_aggregate(aggregates)

    def _update_aggregate(self, aggregate):
        self.aggs_by_id[aggregate.id] = aggregate
        for host in aggregate.hosts:
            self.host_aggregates_map[host].add(aggregate.id)
        for host in self.host_aggregates_map:
            if aggregate.id in self.host_aggregates_map[host] and host not in aggregate.hosts:
                self.host_aggregates_map[host].remove(aggregate.id)

    def delete_aggregate(self, aggregate):
        """Deletes internal HostManager information about a specific aggregate.
        """
        if aggregate.id in self.aggs_by_id:
            del self.aggs_by_id[aggregate.id]
        for host in self.host_aggregates_map:
            if aggregate.id in self.host_aggregates_map[host]:
                self.host_aggregates_map[host].remove(aggregate.id)

    def _init_instance_info(self, computes_by_cell=None):
        """Creates the initial view of instances for all hosts.

        As this initial population of instance information may take some time,
        we don't wish to block the scheduler's startup while this completes.
        The async method allows us to simply mock out the _init_instance_info()
        method in tests.

        :param compute_nodes: a list of nodes to populate instances info for
        if is None, compute_nodes will be looked up in database
        """

        def _async_init_instance_info(computes_by_cell):
            context = context_module.get_admin_context()
            LOG.debug('START:_async_init_instance_info')
            self._instance_info = {}
            count = 0
            if not computes_by_cell:
                computes_by_cell = {}
                for cell in self.cells.values():
                    with context_module.target_cell(context, cell) as cctxt:
                        cell_cns = objects.ComputeNodeList.get_all(cctxt).objects
                        computes_by_cell[cell] = cell_cns
                        count += len(cell_cns)
            LOG.debug('Total number of compute nodes: %s', count)
            for (cell, compute_nodes) in computes_by_cell.items():
                batch_size = 10
                start_node = 0
                end_node = batch_size
                while start_node <= len(compute_nodes):
                    curr_nodes = compute_nodes[start_node:end_node]
                    start_node += batch_size
                    end_node += batch_size
                    filters = {'host': [curr_node.host for curr_node in curr_nodes], 'deleted': False}
                    with context_module.target_cell(context, cell) as cctxt:
                        result = objects.InstanceList.get_by_filters(cctxt, filters)
                    instances = result.objects
                    LOG.debug('Adding %s instances for hosts %s-%s', len(instances), start_node, end_node)
                    for instance in instances:
                        host = instance.host
                        if host not in self._instance_info:
                            self._instance_info[host] = {'instances': {}, 'updated': False}
                        inst_dict = self._instance_info[host]
                        inst_dict['instances'][instance.uuid] = instance
                    time.sleep(0)
                LOG.debug('END:_async_init_instance_info')
        utils.spawn_n(_async_init_instance_info, computes_by_cell)

    def _choose_host_filters(self, filter_cls_names):
        """Since the caller may specify which filters to use we need
        to have an authoritative list of what is permissible. This
        function checks the filter names against a predefined set
        of acceptable filters.
        """
        if not isinstance(filter_cls_names, (list, tuple)):
            filter_cls_names = [filter_cls_names]
        good_filters = []
        bad_filters = []
        for filter_name in filter_cls_names:
            if filter_name not in self.filter_obj_map:
                if filter_name not in self.filter_cls_map:
                    bad_filters.append(filter_name)
                    continue
                filter_cls = self.filter_cls_map[filter_name]
                self.filter_obj_map[filter_name] = filter_cls()
            good_filters.append(self.filter_obj_map[filter_name])
        if bad_filters:
            msg = ', '.join(bad_filters)
            raise exception.SchedulerHostFilterNotFound(filter_name=msg)
        return good_filters

    def get_filtered_hosts(self, hosts, spec_obj, index=0):
        """Filter hosts and return only ones passing all filters."""

        def _strip_ignore_hosts(host_map, hosts_to_ignore):
            ignored_hosts = []
            for host in hosts_to_ignore:
                for (hostname, nodename) in list(host_map.keys()):
                    if host.lower() == hostname.lower():
                        del host_map[hostname, nodename]
                        ignored_hosts.append(host)
            ignored_hosts_str = ', '.join(ignored_hosts)
            LOG.info('Host filter ignoring hosts: %s', ignored_hosts_str)

        def _match_forced_hosts(host_map, hosts_to_force):
            forced_hosts = []
            lowered_hosts_to_force = [host.lower() for host in hosts_to_force]
            for (hostname, nodename) in list(host_map.keys()):
                if hostname.lower() not in lowered_hosts_to_force:
                    del host_map[hostname, nodename]
                else:
                    forced_hosts.append(hostname)
            if host_map:
                forced_hosts_str = ', '.join(forced_hosts)
                LOG.info('Host filter forcing available hosts to %s', forced_hosts_str)
            else:
                forced_hosts_str = ', '.join(hosts_to_force)
                LOG.info("No hosts matched due to not matching 'force_hosts' value of '%s'", forced_hosts_str)

        def _match_forced_nodes(host_map, nodes_to_force):
            forced_nodes = []
            for (hostname, nodename) in list(host_map.keys()):
                if nodename not in nodes_to_force:
                    del host_map[hostname, nodename]
                else:
                    forced_nodes.append(nodename)
            if host_map:
                forced_nodes_str = ', '.join(forced_nodes)
                LOG.info('Host filter forcing available nodes to %s', forced_nodes_str)
            else:
                forced_nodes_str = ', '.join(nodes_to_force)
                LOG.info("No nodes matched due to not matching 'force_nodes' value of '%s'", forced_nodes_str)

        def _get_hosts_matching_request(hosts, requested_destination):
            """Get hosts through matching the requested destination.

            We will both set host and node to requested destination object
            and host will never be None and node will be None in some cases.
            Starting with API 2.74 microversion, we also can specify the
            host/node to select hosts to launch a server:
             - If only host(or only node)(or both host and node) is supplied
               and we get one node from get_compute_nodes_by_host_or_node which
               is called in resources_from_request_spec function,
               the destination will be set both host and node.
             - If only host is supplied and we get more than one node from
               get_compute_nodes_by_host_or_node which is called in
               resources_from_request_spec function, the destination will only
               include host.
            """
            (host, node) = (requested_destination.host, requested_destination.node)
            if node:
                requested_nodes = [x for x in hosts if x.host == host and x.nodename == node]
            else:
                requested_nodes = [x for x in hosts if x.host == host]
            if requested_nodes:
                LOG.info('Host filter only checking host %(host)s and node %(node)s', {'host': host, 'node': node})
            else:
                LOG.info('No hosts matched due to not matching requested destination (%(host)s, %(node)s)', {'host': host, 'node': node})
            return iter(requested_nodes)
        ignore_hosts = spec_obj.ignore_hosts or []
        force_hosts = spec_obj.force_hosts or []
        force_nodes = spec_obj.force_nodes or []
        requested_node = spec_obj.requested_destination
        if requested_node is not None and 'host' in requested_node:
            hosts = _get_hosts_matching_request(hosts, requested_node)
        if ignore_hosts or force_hosts or force_nodes:
            name_to_cls_map = {(x.host, x.nodename): x for x in hosts}
            if ignore_hosts:
                _strip_ignore_hosts(name_to_cls_map, ignore_hosts)
                if not name_to_cls_map:
                    return []
            if force_hosts:
                _match_forced_hosts(name_to_cls_map, force_hosts)
            if force_nodes:
                _match_forced_nodes(name_to_cls_map, force_nodes)
            check_type = 'scheduler_hints' in spec_obj and spec_obj.scheduler_hints.get('_nova_check_type')
            if not check_type and (force_hosts or force_nodes):
                if name_to_cls_map:
                    return name_to_cls_map.values()
                else:
                    return []
            hosts = name_to_cls_map.values()
        return self.filter_handler.get_filtered_objects(self.enabled_filters, hosts, spec_obj, index)

    def get_weighed_hosts(self, hosts, spec_obj):
        """Weigh the hosts."""
        return self.weight_handler.get_weighed_objects(self.weighers, hosts, spec_obj)

    def _get_computes_for_cells(self, context, cells, compute_uuids=None):
        """Get a tuple of compute node and service information.

        :param context: request context
        :param cells: list of CellMapping objects
        :param compute_uuids: list of ComputeNode UUIDs. If this is None, all
            compute nodes from each specified cell will be returned, otherwise
            only the ComputeNode objects with a UUID in the list of UUIDs in
            any given cell is returned. If this is an empty list, the returned
            compute_nodes tuple item will be an empty dict.

        Returns a tuple (compute_nodes, services) where:
         - compute_nodes is cell-uuid keyed dict of compute node lists
         - services is a dict of services indexed by hostname
        """

        def targeted_operation(cctxt):
            services = objects.ServiceList.get_by_binary(cctxt, 'nova-compute', include_disabled=True)
            if compute_uuids is None:
                return (services, objects.ComputeNodeList.get_all(cctxt))
            else:
                return (services, objects.ComputeNodeList.get_all_by_uuids(cctxt, compute_uuids))
        timeout = context_module.CELL_TIMEOUT
        results = context_module.scatter_gather_cells(context, cells, timeout, targeted_operation)
        compute_nodes = collections.defaultdict(list)
        services = {}
        for (cell_uuid, result) in results.items():
            if isinstance(result, Exception):
                LOG.warning('Failed to get computes for cell %s', cell_uuid)
            elif result is context_module.did_not_respond_sentinel:
                LOG.warning('Timeout getting computes for cell %s', cell_uuid)
            else:
                (_services, _compute_nodes) = result
                compute_nodes[cell_uuid].extend(_compute_nodes)
                services.update({service.host: service for service in _services})
        return (compute_nodes, services)

    def _get_cell_by_host(self, ctxt, host):
        """Get CellMapping object of a cell the given host belongs to."""
        try:
            host_mapping = objects.HostMapping.get_by_host(ctxt, host)
            return host_mapping.cell_mapping
        except exception.HostMappingNotFound:
            LOG.warning('No host-to-cell mapping found for selected host %(host)s.', {'host': host})
            return

    def get_compute_nodes_by_host_or_node(self, ctxt, host, node, cell=None):
        """Get compute nodes from given host or node"""

        def return_empty_list_for_not_found(func):

            def wrapper(*args, **kwargs):
                try:
                    ret = func(*args, **kwargs)
                except exception.NotFound:
                    ret = objects.ComputeNodeList()
                return ret
            return wrapper

        @return_empty_list_for_not_found
        def _get_by_host_and_node(ctxt):
            compute_node = objects.ComputeNode.get_by_host_and_nodename(ctxt, host, node)
            return objects.ComputeNodeList(objects=[compute_node])

        @return_empty_list_for_not_found
        def _get_by_host(ctxt):
            return objects.ComputeNodeList.get_all_by_host(ctxt, host)

        @return_empty_list_for_not_found
        def _get_by_node(ctxt):
            compute_node = objects.ComputeNode.get_by_nodename(ctxt, node)
            return objects.ComputeNodeList(objects=[compute_node])
        if host and node:
            target_fnc = _get_by_host_and_node
        elif host:
            target_fnc = _get_by_host
        else:
            target_fnc = _get_by_node
        if host and (not cell):
            cell = self._get_cell_by_host(ctxt, host)
        cells = [cell] if cell else self.enabled_cells
        timeout = context_module.CELL_TIMEOUT
        nodes_by_cell = context_module.scatter_gather_cells(ctxt, cells, timeout, target_fnc)
        nodes = next((nodes for nodes in nodes_by_cell.values() if nodes and (not context_module.is_cell_failure_sentinel(nodes))), objects.ComputeNodeList())
        return nodes

    def refresh_cells_caches(self):
        context = context_module.get_admin_context()
        temp_cells = objects.CellMappingList.get_all(context)
        for c in temp_cells:
            if c.is_cell0():
                temp_cells.objects.remove(c)
                break
        self.cells = {cell.uuid: cell for cell in temp_cells}
        LOG.debug('Found %(count)i cells: %(cells)s', {'count': len(self.cells), 'cells': ', '.join(self.cells)})
        self.enabled_cells = [c for c in temp_cells if not c.disabled]
        if LOG.isEnabledFor(logging.DEBUG):
            disabled_cells = [c for c in temp_cells if c.disabled]
            LOG.debug('Found %(count)i disabled cells: %(cells)s', {'count': len(disabled_cells), 'cells': ', '.join([c.identity for c in disabled_cells])})
        self.host_to_cell_uuid = {}

    def get_host_states_by_uuids(self, context, compute_uuids, spec_obj):
        if not self.cells:
            LOG.warning('No cells were found')
        if spec_obj and 'requested_destination' in spec_obj and spec_obj.requested_destination and ('cell' in spec_obj.requested_destination) and (not spec_obj.requested_destination.allow_cross_cell_move):
            only_cell = spec_obj.requested_destination.cell
        else:
            only_cell = None
        if only_cell:
            cells = [only_cell]
        else:
            cells = self.enabled_cells
        (compute_nodes, services) = self._get_computes_for_cells(context, cells, compute_uuids=compute_uuids)
        return self._get_host_states(context, compute_nodes, services)

    def _get_host_states(self, context, compute_nodes, services):
        """Returns a generator over HostStates given a list of computes.

        Also updates the HostStates internal mapping for the HostManager.
        """
        host_state_map = {}
        seen_nodes = set()
        for (cell_uuid, computes) in compute_nodes.items():
            for compute in computes:
                service = services.get(compute.host)
                if not service:
                    LOG.warning('No compute service record found for host %(host)s', {'host': compute.host})
                    continue
                host = compute.host
                node = compute.hypervisor_hostname
                state_key = (host, node)
                host_state = host_state_map.get(state_key)
                if not host_state:
                    host_state = self.host_state_cls(host, node, cell_uuid, compute=compute)
                    host_state_map[state_key] = host_state
                host_state.update(compute, dict(service), self._get_aggregates_info(host), self._get_instance_info(context, compute))
                seen_nodes.add(state_key)
        return (host_state_map[host] for host in seen_nodes)

    def _get_aggregates_info(self, host):
        return [self.aggs_by_id[agg_id] for agg_id in self.host_aggregates_map[host]]

    def _get_cell_mapping_for_host(self, context, host_name):
        """Finds the CellMapping for a particular host name

        Relies on a cache to quickly fetch the CellMapping if we have looked
        up this host before, otherwise gets the CellMapping via the
        HostMapping record for the given host name.

        :param context: nova auth request context
        :param host_name: compute service host name
        :returns: CellMapping object
        :raises: HostMappingNotFound if the host is not mapped to a cell
        """
        if host_name in self.host_to_cell_uuid:
            cell_uuid = self.host_to_cell_uuid[host_name]
            if cell_uuid in self.cells:
                return self.cells[cell_uuid]
            LOG.warning('Host %s is expected to be in cell %s but that cell uuid was not found in our cache. The service may need to be restarted to refresh the cache.', host_name, cell_uuid)
        hm = objects.HostMapping.get_by_host(context, host_name)
        cell_mapping = hm.cell_mapping
        self.host_to_cell_uuid[host_name] = cell_mapping.uuid
        return cell_mapping

    def _get_instances_by_host(self, context, host_name):
        try:
            cm = self._get_cell_mapping_for_host(context, host_name)
        except exception.HostMappingNotFound:
            LOG.info('Host mapping not found for host %s. Not tracking instance info for this host.', host_name)
            return {}
        with context_module.target_cell(context, cm) as cctxt:
            uuids = objects.InstanceList.get_uuids_by_host(cctxt, host_name)
            return {uuid: objects.Instance(cctxt, uuid=uuid) for uuid in uuids}

    def _get_instance_info(self, context, compute):
        """Gets the host instance info from the compute host.

        Some sites may disable ``track_instance_changes`` for performance or
        isolation reasons. In either of these cases, there will either be no
        information for the host, or the 'updated' value for that host dict
        will be False. In those cases, we need to grab the current InstanceList
        instead of relying on the version in _instance_info.
        """
        host_name = compute.host
        host_info = self._instance_info.get(host_name)
        if host_info and host_info.get('updated'):
            inst_dict = host_info['instances']
        else:
            inst_dict = self._get_instances_by_host(context, host_name)
        return inst_dict

    def _recreate_instance_info(self, context, host_name):
        """Get the InstanceList for the specified host, and store it in the
        _instance_info dict.
        """
        inst_dict = self._get_instances_by_host(context, host_name)
        host_info = self._instance_info[host_name] = {}
        host_info['instances'] = inst_dict
        host_info['updated'] = False

    @utils.synchronized(HOST_INSTANCE_SEMAPHORE)
    def update_instance_info(self, context, host_name, instance_info):
        """Receives an InstanceList object from a compute node.

        This method receives information from a compute node when it starts up,
        or when its instances have changed, and updates its view of hosts and
        instances with it.
        """
        host_info = self._instance_info.get(host_name)
        if host_info:
            inst_dict = host_info.get('instances')
            for instance in instance_info.objects:
                inst_dict[instance.uuid] = instance
            host_info['updated'] = True
        else:
            instances = instance_info.objects
            if len(instances) > 1:
                host_info = self._instance_info[host_name] = {}
                host_info['instances'] = {instance.uuid: instance for instance in instances}
                host_info['updated'] = True
            else:
                self._recreate_instance_info(context, host_name)
                LOG.info("Received an update from an unknown host '%s'. Re-created its InstanceList.", host_name)

    @utils.synchronized(HOST_INSTANCE_SEMAPHORE)
    def delete_instance_info(self, context, host_name, instance_uuid):
        """Receives the UUID from a compute node when one of its instances is
        terminated.

        The instance in the local view of the host's instances is removed.
        """
        host_info = self._instance_info.get(host_name)
        if host_info:
            inst_dict = host_info['instances']
            inst_dict.pop(instance_uuid, None)
            host_info['updated'] = True
        else:
            self._recreate_instance_info(context, host_name)
            LOG.info("Received a delete update from an unknown host '%s'. Re-created its InstanceList.", host_name)

    @utils.synchronized(HOST_INSTANCE_SEMAPHORE)
    def sync_instance_info(self, context, host_name, instance_uuids):
        """Receives the uuids of the instances on a host.

        This method is periodically called by the compute nodes, which send a
        list of all the UUID values for the instances on that node. This is
        used by the scheduler's HostManager to detect when its view of the
        compute node's instances is out of sync.
        """
        host_info = self._instance_info.get(host_name)
        if host_info:
            local_set = set(host_info['instances'].keys())
            compute_set = set(instance_uuids)
            if not local_set == compute_set:
                self._recreate_instance_info(context, host_name)
                LOG.info("The instance sync for host '%s' did not match. Re-created its InstanceList.", host_name)
                return
            host_info['updated'] = True
            LOG.debug("Successfully synced instances from host '%s'.", host_name)
        else:
            self._recreate_instance_info(context, host_name)
            LOG.info("Received a sync request from an unknown host '%s'. Re-created its InstanceList.", host_name)