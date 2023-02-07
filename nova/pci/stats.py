from bees import profiler as p
from oslo_config import cfg
from oslo_log import log as logging
from nova import exception
from nova.objects import fields
from nova.objects import pci_device_pool
from nova.pci import utils
from nova.pci import whitelist
CONF = cfg.CONF
LOG = logging.getLogger(__name__)

@p.trace_cls('PciDeviceStats')
class PciDeviceStats(object):
    """PCI devices summary information.

    According to the PCI SR-IOV spec, a PCI physical function can have up to
    256 PCI virtual functions, thus the number of assignable PCI functions in
    a cloud can be big. The scheduler needs to know all device availability
    information in order to determine which compute hosts can support a PCI
    request. Passing individual virtual device information to the scheduler
    does not scale, so we provide summary information.

    Usually the virtual functions provided by a host PCI device have the same
    value for most properties, like vendor_id, product_id and class type.
    The PCI stats class summarizes this information for the scheduler.

    The pci stats information is maintained exclusively by compute node
    resource tracker and updated to database. The scheduler fetches the
    information and selects the compute node accordingly. If a compute
    node is selected, the resource tracker allocates the devices to the
    instance and updates the pci stats information.

    This summary information will be helpful for cloud management also.
    """
    pool_keys = ['product_id', 'vendor_id', 'numa_node', 'dev_type']

    def __init__(self, numa_topology, stats=None, dev_filter=None):
        super(PciDeviceStats, self).__init__()
        self.numa_topology = numa_topology
        self.pools = [pci_pool.to_dict() for pci_pool in stats] if stats else []
        self.pools.sort(key=lambda item: len(item))
        self.dev_filter = dev_filter or whitelist.Whitelist(CONF.pci.passthrough_whitelist)

    def _equal_properties(self, dev, entry, matching_keys):
        return all((dev.get(prop) == entry.get(prop) for prop in matching_keys))

    def _find_pool(self, dev_pool):
        """Return the first pool that matches dev."""
        for pool in self.pools:
            pool_keys = pool.copy()
            del pool_keys['count']
            del pool_keys['devices']
            if len(pool_keys.keys()) == len(dev_pool.keys()) and self._equal_properties(dev_pool, pool_keys, dev_pool.keys()):
                return pool

    def _create_pool_keys_from_dev(self, dev):
        """create a stats pool dict that this dev is supposed to be part of

        Note that this pool dict contains the stats pool's keys and their
        values. 'count' and 'devices' are not included.
        """
        devspec = self.dev_filter.get_devspec(dev)
        if not devspec:
            return
        tags = devspec.get_tags()
        pool = {k: getattr(dev, k) for k in self.pool_keys}
        if tags:
            pool.update(tags)
        if dev.extra_info.get('parent_ifname'):
            pool['parent_ifname'] = dev.extra_info['parent_ifname']
        return pool

    def _get_pool_with_device_type_mismatch(self, dev):
        """Check for device type mismatch in the pools for a given device.

        Return (pool, device) if device type does not match or a single None
        if the device type matches.
        """
        for pool in self.pools:
            for device in pool['devices']:
                if device.address == dev.address:
                    if dev.dev_type != pool['dev_type']:
                        return (pool, device)
                    return None
        return None

    def update_device(self, dev):
        """Update a device to its matching pool."""
        pool_device_info = self._get_pool_with_device_type_mismatch(dev)
        if pool_device_info is None:
            return
        (pool, device) = pool_device_info
        pool['devices'].remove(device)
        self._decrease_pool_count(self.pools, pool)
        self.add_device(dev)

    def add_device(self, dev):
        """Add a device to its matching pool."""
        dev_pool = self._create_pool_keys_from_dev(dev)
        if dev_pool:
            pool = self._find_pool(dev_pool)
            if not pool:
                dev_pool['count'] = 0
                dev_pool['devices'] = []
                self.pools.append(dev_pool)
                self.pools.sort(key=lambda item: len(item))
                pool = dev_pool
            pool['count'] += 1
            pool['devices'].append(dev)

    @staticmethod
    def _decrease_pool_count(pool_list, pool, count=1):
        """Decrement pool's size by count.

        If pool becomes empty, remove pool from pool_list.
        """
        if pool['count'] > count:
            pool['count'] -= count
            count = 0
        else:
            count -= pool['count']
            pool_list.remove(pool)
        return count

    def remove_device(self, dev):
        """Remove one device from the first pool that it matches."""
        dev_pool = self._create_pool_keys_from_dev(dev)
        if dev_pool:
            pool = self._find_pool(dev_pool)
            if not pool:
                raise exception.PciDevicePoolEmpty(compute_node_id=dev.compute_node_id, address=dev.address)
            pool['devices'].remove(dev)
            self._decrease_pool_count(self.pools, pool)

    def get_free_devs(self):
        free_devs = []
        for pool in self.pools:
            free_devs.extend(pool['devices'])
        return free_devs

    def consume_requests(self, pci_requests, numa_cells=None):
        alloc_devices = []
        for request in pci_requests:
            count = request.count
            pools = self._filter_pools(self.pools, request, numa_cells)
            if not pools:
                LOG.error('Failed to allocate PCI devices for instance. Unassigning devices back to pools. This should not happen, since the scheduler should have accurate information, and allocation during claims is controlled via a hold on the compute node semaphore.')
                for d in range(len(alloc_devices)):
                    self.add_device(alloc_devices.pop())
                return None
            for pool in pools:
                if pool['count'] >= count:
                    num_alloc = count
                else:
                    num_alloc = pool['count']
                count -= num_alloc
                pool['count'] -= num_alloc
                for d in range(num_alloc):
                    pci_dev = pool['devices'].pop()
                    self._handle_device_dependents(pci_dev)
                    pci_dev.request_id = request.request_id
                    alloc_devices.append(pci_dev)
                if count == 0:
                    break
        return alloc_devices

    def _handle_device_dependents(self, pci_dev):
        """Remove device dependents or a parent from pools.

        In case the device is a PF, all of it's dependent VFs should
        be removed from pools count, if these are present.
        When the device is a VF, or a VDPA device, it's parent PF
        pool count should be decreased, unless it is no longer in a pool.
        """
        if pci_dev.dev_type == fields.PciDeviceType.SRIOV_PF:
            vfs_list = pci_dev.child_devices
            if vfs_list:
                for vf in vfs_list:
                    self.remove_device(vf)
        elif pci_dev.dev_type in (fields.PciDeviceType.SRIOV_VF, fields.PciDeviceType.VDPA):
            try:
                parent = pci_dev.parent_device
                if parent in self.get_free_devs():
                    self.remove_device(parent)
            except exception.PciDeviceNotFound:
                return

    def _filter_pools_for_spec(self, pools, request):
        """Filter out pools that don't match the request's device spec.

        Exclude pools that do not match the specified ``vendor_id``,
        ``product_id`` and/or ``device_type`` field, or any of the other
        arbitrary tags such as ``physical_network``, specified in the request.

        :param pools: A list of PCI device pool dicts
        :param request: An InstancePCIRequest object describing the type,
            quantity and required NUMA affinity of device(s) we want.
        :returns: A list of pools that can be used to support the request if
            this is possible.
        """
        request_specs = request.spec
        return [pool for pool in pools if utils.pci_device_prop_match(pool, request_specs)]

    def _filter_pools_for_numa_cells(self, pools, request, numa_cells):
        """Filter out pools with the wrong NUMA affinity, if required.

        Exclude pools that do not have *suitable* PCI NUMA affinity.
        ``numa_policy`` determines what *suitable* means, being one of
        PREFERRED (nice-to-have), LEGACY (must-have-if-available) and REQUIRED
        (must-have). We iterate through the various policies in order of
        strictness. This means that even if we only *prefer* PCI-NUMA affinity,
        we will still attempt to provide it if possible.

        :param pools: A list of PCI device pool dicts
        :param request: An InstancePCIRequest object describing the type,
            quantity and required NUMA affinity of device(s) we want.
        :param numa_cells: A list of InstanceNUMACell objects whose ``id``
            corresponds to the ``id`` of host NUMACells.
        :returns: A list of pools that can, together, provide at least
            ``requested_count`` PCI devices with the level of NUMA affinity
            required by ``numa_policy``, else all pools that can satisfy this
            policy even if it's not enough.
        """
        if not numa_cells:
            return pools
        requested_policy = fields.PCINUMAAffinityPolicy.LEGACY
        if 'numa_policy' in request:
            requested_policy = request.numa_policy or requested_policy
        requested_count = request.count
        numa_cell_ids = [cell.id for cell in numa_cells]
        filtered_pools = [pool for pool in pools if any((utils.pci_device_prop_match(pool, [{'numa_node': cell}]) for cell in numa_cell_ids))]
        if requested_policy == fields.PCINUMAAffinityPolicy.REQUIRED or sum((pool['count'] for pool in filtered_pools)) >= requested_count:
            return filtered_pools
        if requested_policy == fields.PCINUMAAffinityPolicy.SOCKET:
            return self._filter_pools_for_socket_affinity(pools, numa_cells)
        numa_cell_ids.append(None)
        filtered_pools = [pool for pool in pools if any((utils.pci_device_prop_match(pool, [{'numa_node': cell}]) for cell in numa_cell_ids))]
        if requested_policy == fields.PCINUMAAffinityPolicy.LEGACY or sum((pool['count'] for pool in filtered_pools)) >= requested_count:
            return filtered_pools
        return sorted(pools, key=lambda pool: pool.get('numa_node') not in numa_cell_ids)

    def _filter_pools_for_socket_affinity(self, pools, numa_cells):
        host_cells = self.numa_topology.cells
        if any((cell.socket is None for cell in host_cells)):
            LOG.debug('No socket information in host NUMA cell(s).')
            return []
        socket_ids = set()
        for guest_cell in numa_cells:
            for host_cell in host_cells:
                if guest_cell.id == host_cell.id:
                    socket_ids.add(host_cell.socket)
        allowed_numa_nodes = set()
        for host_cell in host_cells:
            if host_cell.socket in socket_ids:
                allowed_numa_nodes.add(host_cell.id)
        return [pool for pool in pools if any((utils.pci_device_prop_match(pool, [{'numa_node': numa_node}]) for numa_node in allowed_numa_nodes))]

    def _filter_pools_for_unrequested_pfs(self, pools, request):
        """Filter out pools with PFs, unless these are required.

        This is necessary in cases where PFs and VFs have the same product_id
        and generally useful elsewhere.

        :param pools: A list of PCI device pool dicts
        :param request: An InstancePCIRequest object describing the type,
            quantity and required NUMA affinity of device(s) we want.
        :returns: A list of pools that can be used to support the request if
            this is possible.
        """
        if all((spec.get('dev_type') != fields.PciDeviceType.SRIOV_PF for spec in request.spec)):
            pools = [pool for pool in pools if not pool.get('dev_type') == fields.PciDeviceType.SRIOV_PF]
        return pools

    def _filter_pools_for_unrequested_vdpa_devices(self, pools, request):
        """Filter out pools with VDPA devices, unless these are required.

        This is necessary as vdpa devices require special handling and
        should not be allocated to generic pci device requests.

        :param pools: A list of PCI device pool dicts
        :param request: An InstancePCIRequest object describing the type,
            quantity and required NUMA affinity of device(s) we want.
        :returns: A list of pools that can be used to support the request if
            this is possible.
        """
        if all((spec.get('dev_type') != fields.PciDeviceType.VDPA for spec in request.spec)):
            pools = [pool for pool in pools if not pool.get('dev_type') == fields.PciDeviceType.VDPA]
        return pools

    def _filter_pools(self, pools, request, numa_cells):
        """Determine if an individual PCI request can be met.

        Filter pools, which are collections of devices with similar traits, to
        identify those that can support the provided PCI request.

        If ``numa_cells`` is provided then NUMA locality may be taken into
        account, depending on the value of ``request.numa_policy``.

        :param pools: A list of PCI device pool dicts
        :param request: An InstancePCIRequest object describing the type,
            quantity and required NUMA affinity of device(s) we want.
        :param numa_cells: A list of InstanceNUMACell objects whose ``id``
            corresponds to the ``id`` of host NUMACell objects.
        :returns: A list of pools that can be used to support the request if
            this is possible, else None.
        """
        before_count = sum([pool['count'] for pool in pools])
        pools = self._filter_pools_for_spec(pools, request)
        after_count = sum([pool['count'] for pool in pools])
        if after_count < before_count:
            LOG.debug('Dropped %d device(s) due to mismatched PCI attribute(s)', before_count - after_count)
        if after_count < request.count:
            LOG.debug('Not enough PCI devices left to satisfy request')
            return None
        before_count = after_count
        pools = self._filter_pools_for_numa_cells(pools, request, numa_cells)
        after_count = sum([pool['count'] for pool in pools])
        if after_count < before_count:
            LOG.debug('Dropped %d device(s) as they are on the wrong NUMA node(s)', before_count - after_count)
        if after_count < request.count:
            LOG.debug('Not enough PCI devices left to satisfy request')
            return None
        before_count = after_count
        pools = self._filter_pools_for_unrequested_pfs(pools, request)
        after_count = sum([pool['count'] for pool in pools])
        if after_count < before_count:
            LOG.debug('Dropped %d device(s) as they are PFs which we have not requested', before_count - after_count)
        if after_count < request.count:
            LOG.debug('Not enough PCI devices left to satisfy request')
            return None
        before_count = after_count
        pools = self._filter_pools_for_unrequested_vdpa_devices(pools, request)
        after_count = sum([pool['count'] for pool in pools])
        if after_count < before_count:
            LOG.debug('Dropped %d device(s) as they are VDPA devices which we have not requested', before_count - after_count)
        if after_count < request.count:
            LOG.debug('Not enough PCI devices left to satisfy request')
            return None
        return pools

    def support_requests(self, requests, numa_cells=None):
        """Determine if the PCI requests can be met.

        Determine, based on a compute node's PCI stats, if an instance can be
        scheduled on the node. **Support does not mean real allocation**.

        If ``numa_cells`` is provided then NUMA locality may be taken into
        account, depending on the value of ``numa_policy``.

        :param requests: A list of InstancePCIRequest object describing the
            types, quantities and required NUMA affinities of devices we want.
        :type requests: nova.objects.InstancePCIRequests
        :param numa_cells: A list of InstanceNUMACell objects whose ``id``
            corresponds to the ``id`` of host NUMACells, or None.
        :returns: Whether this compute node can satisfy the given request.
        """
        return all((self._filter_pools(self.pools, r, numa_cells) for r in requests))

    def _apply_request(self, pools, request, numa_cells=None):
        """Apply an individual PCI request.

        Apply a PCI request against a given set of PCI device pools, which are
        collections of devices with similar traits.

        If ``numa_cells`` is provided then NUMA locality may be taken into
        account, depending on the value of ``request.numa_policy``.

        :param pools: A list of PCI device pool dicts
        :param request: An InstancePCIRequest object describing the type,
            quantity and required NUMA affinity of device(s) we want.
        :param numa_cells: A list of InstanceNUMACell objects whose ``id``
            corresponds to the ``id`` of host NUMACell objects.
        :returns: True if the request was applied against the provided pools
            successfully, else False.
        """
        filtered_pools = self._filter_pools(pools, request, numa_cells)
        if not filtered_pools:
            return False
        count = request.count
        for pool in filtered_pools:
            count = self._decrease_pool_count(pools, pool, count)
            if not count:
                break
        return True

    def apply_requests(self, requests, numa_cells=None):
        """Apply PCI requests to the PCI stats.

        This is used in multiple instance creation, when the scheduler has to
        maintain how the resources are consumed by the instances.

        If ``numa_cells`` is provided then NUMA locality may be taken into
        account, depending on the value of ``numa_policy``.

        :param requests: A list of InstancePCIRequest object describing the
            types, quantities and required NUMA affinities of devices we want.
        :type requests: nova.objects.InstancePCIRequests
        :param numa_cells: A list of InstanceNUMACell objects whose ``id``
            corresponds to the ``id`` of host NUMACells, or None.
        :raises: exception.PciDeviceRequestFailed if this compute node cannot
            satisfy the given request.
        """
        if not all((self._apply_request(self.pools, r, numa_cells) for r in requests)):
            raise exception.PciDeviceRequestFailed(requests=requests)

    def __iter__(self):
        pools = []
        for pool in self.pools:
            tmp = {k: v for (k, v) in pool.items() if k != 'devices'}
            pools.append(tmp)
        return iter(pools)

    def clear(self):
        """Clear all the stats maintained."""
        self.pools = []

    def __eq__(self, other):
        return self.pools == other.pools

    def to_device_pools_obj(self):
        """Return the contents of the pools as a PciDevicePoolList object."""
        stats = [x for x in self]
        return pci_device_pool.from_pci_stats(stats)