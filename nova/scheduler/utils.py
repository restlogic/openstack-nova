from bees import profiler as p
'Utility methods for scheduling.'
import collections
import re
import sys
import typing as ty
from urllib import parse
import os_resource_classes as orc
import os_traits
from oslo_log import log as logging
from oslo_serialization import jsonutils
from nova.compute import flavors
from nova.compute import utils as compute_utils
import nova.conf
from nova import context as nova_context
from nova import exception
from nova.i18n import _
from nova import objects
from nova.objects import base as obj_base
from nova.objects import fields as obj_fields
from nova.objects import instance as obj_instance
from nova import rpc
from nova.scheduler.filters import utils as filters_utils
from nova.virt import hardware
LOG = logging.getLogger(__name__)
CONF = nova.conf.CONF
GroupDetails = collections.namedtuple('GroupDetails', ['hosts', 'policy', 'members'])

@p.trace_cls('ResourceRequest')
class ResourceRequest(object):
    """Presents a granular resource request via RequestGroup instances."""
    XS_RES_PREFIX = 'resources'
    XS_TRAIT_PREFIX = 'trait'
    XS_KEYPAT = re.compile('^(%s)([a-zA-Z0-9_-]{1,64})?:(.*)$' % '|'.join((XS_RES_PREFIX, XS_TRAIT_PREFIX)))

    def __init__(self):
        """Create an empty ResourceRequest

        Do not call this directly, use the existing static factory methods
        from_*()
        """
        self._rg_by_id: ty.Dict[str, objects.RequestGroup] = {}
        self._group_policy: ty.Optional[str] = None
        self._limit = CONF.scheduler.max_placement_results
        self._root_required: ty.Set[str] = set()
        self._root_forbidden: ty.Set[str] = set()
        self.suffixed_groups_from_flavor = 0
        self.cpu_pinning_requested = False

    @classmethod
    def from_request_spec(cls, request_spec: 'objects.RequestSpec', enable_pinning_translate: bool=True) -> 'ResourceRequest':
        """Create a new instance of ResourceRequest from a RequestSpec.

        Examines the flavor, flavor extra specs, (optional) image metadata,
        and (optional) requested_resources and request_level_params of the
        provided ``request_spec``.

        For extra specs, items of the following form are examined:

        - ``resources:$RESOURCE_CLASS``: $AMOUNT
        - ``resources$S:$RESOURCE_CLASS``: $AMOUNT
        - ``trait:$TRAIT_NAME``: "required"
        - ``trait$S:$TRAIT_NAME``: "required"

        ...where ``$S`` is a string suffix as supported via Placement
        microversion 1.33
        https://docs.openstack.org/placement/train/specs/train/implemented/2005575-nested-magic-1.html#arbitrary-group-suffixes

        .. note::

            This does *not* yet handle ``member_of[$S]``.

        The string suffix is used as the RequestGroup.requester_id to
        facilitate mapping of requests to allocation candidates using the
        ``mappings`` piece of the response added in Placement microversion 1.34
        https://docs.openstack.org/placement/train/specs/train/implemented/placement-resource-provider-request-group-mapping-in-allocation-candidates.html

        For image metadata, traits are extracted from the ``traits_required``
        property, if present.

        For the flavor, ``VCPU``, ``MEMORY_MB`` and ``DISK_GB`` are calculated
        from Flavor properties, though these are only used if they aren't
        overridden by flavor extra specs.

        requested_resources, which are existing RequestGroup instances created
        on the RequestSpec based on resources specified outside of the flavor/
        image (e.g. from ports) are incorporated as is, but ensuring that they
        get unique group suffixes.

        request_level_params - settings associated with the request as a whole
        rather than with a specific RequestGroup - are incorporated as is.

        :param request_spec: An instance of ``objects.RequestSpec``.
        :param enable_pinning_translate: True if the CPU policy extra specs
            should be translated to placement resources and traits.
        :return: a ResourceRequest instance
        """
        res_req = cls()
        res_req._root_required = request_spec.root_required
        res_req._root_forbidden = request_spec.root_forbidden
        if 'image' in request_spec and request_spec.image:
            image = request_spec.image
        else:
            image = objects.ImageMeta(properties=objects.ImageMetaProps())
        res_req._process_extra_specs(request_spec.flavor)
        res_req.suffixed_groups_from_flavor = res_req.get_num_of_suffixed_groups()
        res_req._process_image_meta(image)
        if enable_pinning_translate:
            res_req._translate_pinning_policies(request_spec.flavor, image)
        res_req._process_requested_resources(request_spec)
        merged_resources = res_req.merged_resources()
        if orc.VCPU not in merged_resources and orc.PCPU not in merged_resources:
            res_req._add_resource(orc.VCPU, request_spec.vcpus)
        if orc.MEMORY_MB not in merged_resources:
            res_req._add_resource(orc.MEMORY_MB, request_spec.memory_mb)
        if orc.DISK_GB not in merged_resources:
            disk = request_spec.ephemeral_gb
            disk += compute_utils.convert_mb_to_ceil_gb(request_spec.swap)
            if 'is_bfv' not in request_spec or not request_spec.is_bfv:
                disk += request_spec.root_gb
            if disk:
                res_req._add_resource(orc.DISK_GB, disk)
        res_req._translate_memory_encryption(request_spec.flavor, image)
        res_req._translate_vpmems_request(request_spec.flavor)
        res_req._translate_vtpm_request(request_spec.flavor, image)
        res_req._translate_pci_numa_affinity_policy(request_spec.flavor, image)
        res_req._translate_secure_boot_request(request_spec.flavor, image)
        res_req.strip_zeros()
        return res_req

    @classmethod
    def from_request_group(cls, request_group: 'objects.RequestGroup') -> 'ResourceRequest':
        """Create a new instance of ResourceRequest from a RequestGroup."""
        res_req = cls()
        res_req._add_request_group(request_group)
        res_req.strip_zeros()
        return res_req

    def _process_requested_resources(self, request_spec):
        requested_resources = request_spec.requested_resources if 'requested_resources' in request_spec and request_spec.requested_resources else []
        for group in requested_resources:
            self._add_request_group(group)

    def _process_extra_specs(self, flavor):
        if 'extra_specs' not in flavor:
            return
        for (key, val) in flavor.extra_specs.items():
            if key == 'group_policy':
                self._add_group_policy(val)
                continue
            match = self.XS_KEYPAT.match(key)
            if not match:
                continue
            (prefix, suffix, name) = match.groups()
            if prefix == self.XS_RES_PREFIX:
                self._add_resource(name, val, group=suffix)
            elif prefix == self.XS_TRAIT_PREFIX:
                self._add_trait(name, val, group=suffix)

    def _process_image_meta(self, image):
        if not image or 'properties' not in image:
            return
        for trait in image.properties.get('traits_required', []):
            self._add_trait(trait, 'required')

    def _translate_secure_boot_request(self, flavor, image):
        sb_policy = hardware.get_secure_boot_constraint(flavor, image)
        if sb_policy != obj_fields.SecureBoot.REQUIRED:
            return
        trait = os_traits.COMPUTE_SECURITY_UEFI_SECURE_BOOT
        self._add_trait(trait, 'required')
        LOG.debug('Requiring secure boot support via trait %s.', trait)

    def _translate_vtpm_request(self, flavor, image):
        vtpm_config = hardware.get_vtpm_constraint(flavor, image)
        if not vtpm_config:
            return
        if vtpm_config.version == obj_fields.TPMVersion.v1_2:
            trait = os_traits.COMPUTE_SECURITY_TPM_1_2
        else:
            trait = os_traits.COMPUTE_SECURITY_TPM_2_0
        self._add_trait(trait, 'required')
        LOG.debug('Requiring emulated TPM support via trait %s.', trait)

    def _translate_memory_encryption(self, flavor, image):
        """When the hw:mem_encryption extra spec or the hw_mem_encryption
        image property are requested, translate into a request for
        resources:MEM_ENCRYPTION_CONTEXT=1 which requires a slot on a
        host which can support encryption of the guest memory.
        """
        if not hardware.get_mem_encryption_constraint(flavor, image):
            return
        self._add_resource(orc.MEM_ENCRYPTION_CONTEXT, 1)
        LOG.debug('Added %s=1 to requested resources', orc.MEM_ENCRYPTION_CONTEXT)

    def _translate_vpmems_request(self, flavor):
        """When the hw:pmem extra spec is present, require hosts which can
        provide enough vpmem resources.
        """
        vpmem_labels = hardware.get_vpmems(flavor)
        if not vpmem_labels:
            return
        amount_by_rc: ty.DefaultDict[str, int] = collections.defaultdict(int)
        for vpmem_label in vpmem_labels:
            resource_class = orc.normalize_name('PMEM_NAMESPACE_' + vpmem_label)
            amount_by_rc[resource_class] += 1
        for (resource_class, amount) in amount_by_rc.items():
            self._add_resource(resource_class, amount)
            LOG.debug('Added resource %s=%d to requested resources', resource_class, amount)

    def _translate_pinning_policies(self, flavor, image):
        """Translate the legacy pinning policies to resource requests."""
        cpu_policy = hardware.get_cpu_policy_constraint(flavor, image)
        cpu_thread_policy = hardware.get_cpu_thread_policy_constraint(flavor, image)
        emul_thread_policy = hardware.get_emulator_thread_policy_constraint(flavor)
        if cpu_policy == obj_fields.CPUAllocationPolicy.DEDICATED:
            self.cpu_pinning_requested = True
            pcpus = flavor.vcpus
            LOG.debug('Translating request for %(vcpu_rc)s=%(pcpus)d to %(vcpu_rc)s=0,%(pcpu_rc)s=%(pcpus)d', {'vcpu_rc': orc.VCPU, 'pcpu_rc': orc.PCPU, 'pcpus': pcpus})
        if cpu_policy == obj_fields.CPUAllocationPolicy.MIXED:
            dedicated_cpus = hardware.get_dedicated_cpu_constraint(flavor)
            realtime_cpus = hardware.get_realtime_cpu_constraint(flavor, image)
            pcpus = len(dedicated_cpus or realtime_cpus or [])
            vcpus = flavor.vcpus - pcpus
            self._add_resource(orc.VCPU, vcpus)
        if cpu_policy in (obj_fields.CPUAllocationPolicy.DEDICATED, obj_fields.CPUAllocationPolicy.MIXED):
            if emul_thread_policy == 'isolate':
                pcpus += 1
                LOG.debug('Adding additional %(pcpu_rc)s to account for emulator threads', {'pcpu_rc': orc.PCPU})
            self._add_resource(orc.PCPU, pcpus)
        trait = {obj_fields.CPUThreadAllocationPolicy.ISOLATE: 'forbidden', obj_fields.CPUThreadAllocationPolicy.REQUIRE: 'required'}.get(cpu_thread_policy)
        if trait:
            LOG.debug('Adding %(trait)s=%(value)s trait', {'trait': os_traits.HW_CPU_HYPERTHREADING, 'value': trait})
            self._add_trait(os_traits.HW_CPU_HYPERTHREADING, trait)

    def _translate_pci_numa_affinity_policy(self, flavor, image):
        policy = hardware.get_pci_numa_policy_constraint(flavor, image)
        if policy == objects.fields.PCINUMAAffinityPolicy.SOCKET:
            trait = os_traits.COMPUTE_SOCKET_PCI_NUMA_AFFINITY
            self._add_trait(trait, 'required')
            LOG.debug("Requiring 'socket' PCI NUMA affinity support via trait %s.", trait)

    @property
    def group_policy(self):
        return self._group_policy

    @group_policy.setter
    def group_policy(self, value):
        self._group_policy = value

    def get_request_group(self, ident):
        if ident not in self._rg_by_id:
            rq_grp = objects.RequestGroup(use_same_provider=bool(ident), requester_id=ident)
            self._rg_by_id[ident] = rq_grp
        return self._rg_by_id[ident]

    def _add_request_group(self, request_group):
        """Inserts the existing group with a unique suffix.

        The groups coming from the flavor can have arbitrary suffixes; those
        are guaranteed to be unique within the flavor.

        A group coming from "outside" (ports, device profiles) must be
        associated with a requester_id, such as a port UUID. We use this
        requester_id as the group suffix (but ensure that it is unique in
        combination with suffixes from the flavor).

        Groups coming from "outside" are not allowed to be no-ops. That is,
        they must provide resources and/or required/forbidden traits/aggregates

        :param request_group: the RequestGroup to be added.
        :raise: ValueError if request_group has no requester_id, or if it
            provides no resources or (required/forbidden) traits or aggregates.
        :raise: RequestGroupSuffixConflict if request_group.requester_id
            already exists in this ResourceRequest.
        """
        if not request_group.requester_id:
            raise ValueError(_('Missing requester_id in RequestGroup! This is probably a programmer error. %s') % request_group)
        if request_group.is_empty():
            raise ValueError(_('Refusing to add no-op RequestGroup with requester_id=%s. This is a probably a programmer error.') % request_group.requester_id)
        if request_group.requester_id in self._rg_by_id:
            raise exception.RequestGroupSuffixConflict(suffix=request_group.requester_id)
        self._rg_by_id[request_group.requester_id] = request_group

    def _add_resource(self, rclass, amount, group=None):
        """Add resource request to specified request group.

        Defaults to the unsuffixed request group if no group is provided.
        """
        self.get_request_group(group).add_resource(rclass, amount)

    def _add_trait(self, trait_name, trait_type, group=None):
        """Add trait request to specified group.

        Defaults to the unsuffixed request group if no group is provided.
        """
        self.get_request_group(group).add_trait(trait_name, trait_type)

    def _add_group_policy(self, policy):
        if policy not in ('none', 'isolate'):
            LOG.warning("Invalid group_policy '%s'. Valid values are 'none' and 'isolate'.", policy)
            return
        self._group_policy = policy

    def get_num_of_suffixed_groups(self):
        return len([ident for ident in self._rg_by_id.keys() if ident is not None])

    def merged_resources(self):
        """Returns a merge of {resource_class: amount} for all resource groups.

        Amounts of the same resource class from different groups are added
        together.

        :return: A dict of the form {resource_class: amount}
        """
        ret: ty.DefaultDict[str, int] = collections.defaultdict(lambda : 0)
        for rg in self._rg_by_id.values():
            for (resource_class, amount) in rg.resources.items():
                ret[resource_class] += amount
        return dict(ret)

    def strip_zeros(self):
        """Remove any resources whose amounts are zero."""
        for rg in self._rg_by_id.values():
            rg.strip_zeros()
        for (ident, rg) in list(self._rg_by_id.items()):
            if rg.is_empty():
                self._rg_by_id.pop(ident)

    def to_querystring(self):
        """Produce a querystring of the form expected by
        GET /allocation_candidates.
        """
        if self._limit is not None:
            qparams = [('limit', self._limit)]
        else:
            qparams = []
        if self._group_policy is not None:
            qparams.append(('group_policy', self._group_policy))
        if self._root_required or self._root_forbidden:
            vals = sorted(self._root_required) + ['!' + t for t in sorted(self._root_forbidden)]
            qparams.append(('root_required', ','.join(vals)))
        for rg in self._rg_by_id.values():
            qparams.extend(rg.to_queryparams())
        return parse.urlencode(sorted(qparams))

    @property
    def all_required_traits(self):
        traits: ty.Set[str] = set()
        for rr in self._rg_by_id.values():
            traits = traits.union(rr.required_traits)
        return traits

    def __str__(self):
        return ', '.join(sorted(list((str(rg) for rg in list(self._rg_by_id.values())))))

@p.trace('build_request_spec')
def build_request_spec(image, instances, instance_type=None):
    """Build a request_spec (ahem, not a RequestSpec) for the scheduler.

    The request_spec assumes that all instances to be scheduled are the same
    type.

    :param image: optional primitive image meta dict
    :param instances: list of instances; objects will be converted to
        primitives
    :param instance_type: optional flavor; objects will be converted to
        primitives
    :return: dict with the following keys::

        'image': the image dict passed in or {}
        'instance_properties': primitive version of the first instance passed
        'instance_type': primitive version of the instance_type or None
        'num_instances': the number of instances passed in
    """
    instance = instances[0]
    if instance_type is None:
        if isinstance(instance, obj_instance.Instance):
            instance_type = instance.get_flavor()
        else:
            instance_type = flavors.extract_flavor(instance)
    if isinstance(instance, obj_instance.Instance):
        instance = obj_base.obj_to_primitive(instance)
        instance['system_metadata'] = dict(instance.get('system_metadata', {}))
    if isinstance(instance_type, objects.Flavor):
        instance_type = obj_base.obj_to_primitive(instance_type)
        try:
            flavors.save_flavor_info(instance.get('system_metadata', {}), instance_type)
        except KeyError:
            pass
    request_spec = {'image': image or {}, 'instance_properties': instance, 'instance_type': instance_type, 'num_instances': len(instances)}
    return jsonutils.to_primitive(request_spec)

@p.trace('resources_from_flavor')
def resources_from_flavor(instance, flavor):
    """Convert a flavor into a set of resources for placement, taking into
    account boot-from-volume instances.

    This takes an instance and a flavor and returns a dict of
    resource_class:amount based on the attributes of the flavor, accounting for
    any overrides that are made in extra_specs.
    """
    is_bfv = compute_utils.is_volume_backed_instance(instance._context, instance)
    req_spec = objects.RequestSpec(flavor=flavor, is_bfv=is_bfv)
    res_req = ResourceRequest.from_request_spec(req_spec)
    return res_req.merged_resources()

@p.trace('resources_from_request_spec')
def resources_from_request_spec(ctxt, spec_obj, host_manager, enable_pinning_translate=True):
    """Given a RequestSpec object, returns a ResourceRequest of the resources,
    traits, and aggregates it represents.

    :param context: The request context.
    :param spec_obj: A RequestSpec object.
    :param host_manager: A HostManager object.
    :param enable_pinning_translate: True if the CPU policy extra specs should
        be translated to placement resources and traits.

    :return: A ResourceRequest object.
    :raises NoValidHost: If the specified host/node is not found in the DB.
    """
    res_req = ResourceRequest.from_request_spec(spec_obj, enable_pinning_translate)
    target_host = None
    target_node = None
    target_cell = None
    if 'requested_destination' in spec_obj:
        destination = spec_obj.requested_destination
        if destination:
            if 'host' in destination:
                target_host = destination.host
            if 'node' in destination:
                target_node = destination.node
            if 'cell' in destination:
                target_cell = destination.cell
            if destination.aggregates:
                grp = res_req.get_request_group(None)
                grp.aggregates = [ored.split(',') for ored in destination.aggregates]
            if destination.forbidden_aggregates:
                grp = res_req.get_request_group(None)
                grp.forbidden_aggregates |= destination.forbidden_aggregates
    if 'force_hosts' in spec_obj and spec_obj.force_hosts:
        target_host = target_host or spec_obj.force_hosts[0]
    if 'force_nodes' in spec_obj and spec_obj.force_nodes:
        target_node = target_node or spec_obj.force_nodes[0]
    if target_host or target_node:
        nodes = host_manager.get_compute_nodes_by_host_or_node(ctxt, target_host, target_node, cell=target_cell)
        if not nodes:
            reason = _('No such host - host: %(host)s node: %(node)s ') % {'host': target_host, 'node': target_node}
            raise exception.NoValidHost(reason=reason)
        if len(nodes) == 1:
            if 'requested_destination' in spec_obj and destination:
                destination.host = nodes[0].host
                destination.node = nodes[0].hypervisor_hostname
            grp = res_req.get_request_group(None)
            grp.in_tree = nodes[0].uuid
        else:
            res_req._limit = None
    if 'scheduler_hints' in spec_obj and any((key in ['group', 'same_host', 'different_host'] for key in spec_obj.scheduler_hints)):
        res_req._limit = None
    if res_req.get_num_of_suffixed_groups() >= 2 and (not res_req.group_policy):
        LOG.warning("There is more than one numbered request group in the allocation candidate query but the flavor did not specify any group policy. This query would fail in placement due to the missing group policy. If you specified more than one numbered request group in the flavor extra_spec then you need to specify the group policy in the flavor extra_spec. If it is OK to let these groups be satisfied by overlapping resource providers then use 'group_policy': 'none'. If you want each group to be satisfied from a separate resource provider then use 'group_policy': 'isolate'.")
        if res_req.suffixed_groups_from_flavor <= 1:
            LOG.info("At least one numbered request group is defined outside of the flavor (e.g. in a port that has a QoS minimum bandwidth policy rule attached) but the flavor did not specify any group policy. To avoid the placement failure nova defaults the group policy to 'none'.")
            res_req.group_policy = 'none'
    return res_req

@p.trace('claim_resources_on_destination')
def claim_resources_on_destination(context, reportclient, instance, source_node, dest_node, source_allocations=None, consumer_generation=None):
    """Copies allocations from source node to dest node in Placement

    Normally the scheduler will allocate resources on a chosen destination
    node during a move operation like evacuate and live migration. However,
    because of the ability to force a host and bypass the scheduler, this
    method can be used to manually copy allocations from the source node to
    the forced destination node.

    This is only appropriate when the instance flavor on the source node
    is the same on the destination node, i.e. don't use this for resize.

    :param context: The request context.
    :param reportclient: An instance of the SchedulerReportClient.
    :param instance: The instance being moved.
    :param source_node: source ComputeNode where the instance currently
                        lives
    :param dest_node: destination ComputeNode where the instance is being
                      moved
    :param source_allocations: The consumer's current allocations on the
                               source compute
    :param consumer_generation: The expected generation of the consumer.
                                None if a new consumer is expected
    :raises NoValidHost: If the allocation claim on the destination
                         node fails.
    :raises: keystoneauth1.exceptions.base.ClientException on failure to
             communicate with the placement API
    :raises: ConsumerAllocationRetrievalFailed if the placement API call fails
    :raises: AllocationUpdateFailed: If a parallel consumer update changed the
                                     consumer
    """
    if not source_allocations:
        allocations = reportclient.get_allocs_for_consumer(context, instance.uuid)
        source_allocations = allocations.get('allocations', {})
        consumer_generation = allocations.get('consumer_generation')
        if not source_allocations:
            raise exception.ConsumerAllocationRetrievalFailed(consumer_uuid=instance.uuid, error=_('Expected to find allocations for source node resource provider %s. Retry the operation without forcing a destination host.') % source_node.uuid)
    if len(source_allocations) > 1:
        reason = _('Unable to move instance %(instance_uuid)s to host %(host)s. The instance has complex allocations on the source host so move cannot be forced.') % {'instance_uuid': instance.uuid, 'host': dest_node.host}
        raise exception.NoValidHost(reason=reason)
    alloc_request = {'allocations': {dest_node.uuid: {'resources': source_allocations[source_node.uuid]['resources']}}}
    from nova.scheduler.client import report
    if reportclient.claim_resources(context, instance.uuid, alloc_request, instance.project_id, instance.user_id, allocation_request_version=report.CONSUMER_GENERATION_VERSION, consumer_generation=consumer_generation):
        LOG.debug('Instance allocations successfully created on destination node %(dest)s: %(alloc_request)s', {'dest': dest_node.uuid, 'alloc_request': alloc_request}, instance=instance)
    else:
        reason = _('Unable to move instance %(instance_uuid)s to host %(host)s. There is not enough capacity on the host for the instance.') % {'instance_uuid': instance.uuid, 'host': dest_node.host}
        raise exception.NoValidHost(reason=reason)

@p.trace('set_vm_state_and_notify')
def set_vm_state_and_notify(context, instance_uuid, service, method, updates, ex, request_spec):
    """Updates the instance, sets the fault and sends an error notification.

    :param context: The request context.
    :param instance_uuid: The UUID of the instance to update.
    :param service: The name of the originating service, e.g. 'compute_task'.
        This becomes part of the publisher_id for the notification payload.
    :param method: The method that failed, e.g. 'migrate_server'.
    :param updates: dict of updates for the instance object, typically a
        vm_state and task_state value.
    :param ex: An exception which occurred during the given method.
    :param request_spec: Optional request spec.
    """
    LOG.warning('Failed to %(service)s_%(method)s: %(ex)s', {'service': service, 'method': method, 'ex': ex})
    if request_spec is not None:
        if isinstance(request_spec, objects.RequestSpec):
            request_spec = request_spec.to_legacy_request_spec_dict()
    else:
        request_spec = {}
    vm_state = updates['vm_state']
    properties = request_spec.get('instance_properties', {})
    notifier = rpc.get_notifier(service)
    state = vm_state.upper()
    LOG.warning('Setting instance to %s state.', state, instance_uuid=instance_uuid)
    instance = objects.Instance(context=context, uuid=instance_uuid, **updates)
    instance.obj_reset_changes(['uuid'])
    instance.save()
    compute_utils.add_instance_fault_from_exc(context, instance, ex, sys.exc_info())
    payload = dict(request_spec=request_spec, instance_properties=properties, instance_id=instance_uuid, state=vm_state, method=method, reason=ex)
    event_type = '%s.%s' % (service, method)
    notifier.error(context, event_type, payload)
    compute_utils.notify_about_compute_task_error(context, method, instance_uuid, request_spec, vm_state, ex)

@p.trace('build_filter_properties')
def build_filter_properties(scheduler_hints, forced_host, forced_node, instance_type):
    """Build the filter_properties dict from data in the boot request."""
    filter_properties = dict(scheduler_hints=scheduler_hints)
    filter_properties['instance_type'] = instance_type
    if forced_host:
        filter_properties['force_hosts'] = [forced_host]
    if forced_node:
        filter_properties['force_nodes'] = [forced_node]
    return filter_properties

@p.trace('populate_filter_properties')
def populate_filter_properties(filter_properties, selection):
    """Add additional information to the filter properties after a node has
    been selected by the scheduling process.

    :param filter_properties: dict of filter properties (the legacy form of
        the RequestSpec)
    :param selection: Selection object
    """
    host = selection.service_host
    nodename = selection.nodename
    if 'limits' in selection and selection.limits is not None:
        limits = selection.limits.to_dict()
    else:
        limits = {}
    _add_retry_host(filter_properties, host, nodename)
    if not filter_properties.get('force_hosts'):
        filter_properties['limits'] = limits

@p.trace('populate_retry')
def populate_retry(filter_properties, instance_uuid):
    max_attempts = CONF.scheduler.max_attempts
    force_hosts = filter_properties.get('force_hosts', [])
    force_nodes = filter_properties.get('force_nodes', [])
    if max_attempts == 1 or len(force_hosts) == 1 or len(force_nodes) == 1:
        if max_attempts == 1:
            LOG.debug('Re-scheduling is disabled due to "max_attempts" config')
        else:
            LOG.debug('Re-scheduling is disabled due to forcing a host (%s) and/or node (%s)', force_hosts, force_nodes)
        return
    retry = filter_properties.setdefault('retry', {'num_attempts': 0, 'hosts': []})
    retry['num_attempts'] += 1
    _log_compute_error(instance_uuid, retry)
    exc_reason = retry.pop('exc_reason', None)
    if retry['num_attempts'] > max_attempts:
        msg = _('Exceeded max scheduling attempts %(max_attempts)d for instance %(instance_uuid)s. Last exception: %(exc_reason)s') % {'max_attempts': max_attempts, 'instance_uuid': instance_uuid, 'exc_reason': exc_reason}
        raise exception.MaxRetriesExceeded(reason=msg)

@p.trace('_log_compute_error')
def _log_compute_error(instance_uuid, retry):
    """If the request contained an exception from a previous compute
    build/resize operation, log it to aid debugging
    """
    exc = retry.get('exc')
    if not exc:
        return
    hosts = retry.get('hosts', None)
    if not hosts:
        return
    (last_host, last_node) = hosts[-1]
    LOG.error('Error from last host: %(last_host)s (node %(last_node)s): %(exc)s', {'last_host': last_host, 'last_node': last_node, 'exc': exc}, instance_uuid=instance_uuid)

@p.trace('_add_retry_host')
def _add_retry_host(filter_properties, host, node):
    """Add a retry entry for the selected compute node. In the event that
    the request gets re-scheduled, this entry will signal that the given
    node has already been tried.
    """
    retry = filter_properties.get('retry', None)
    if not retry:
        return
    hosts = retry['hosts']
    hosts.append([host, node])

@p.trace('parse_options')
def parse_options(opts, sep='=', converter=str, name=''):
    """Parse a list of options, each in the format of <key><sep><value>. Also
    use the converter to convert the value into desired type.

    :params opts: list of options, e.g. from oslo_config.cfg.ListOpt
    :params sep: the separator
    :params converter: callable object to convert the value, should raise
                       ValueError for conversion failure
    :params name: name of the option

    :returns: a lists of tuple of values (key, converted_value)
    """
    good = []
    bad = []
    for opt in opts:
        try:
            (key, seen_sep, value) = opt.partition(sep)
            value = converter(value)
        except ValueError:
            key = None
            value = None
        if key and seen_sep and (value is not None):
            good.append((key, value))
        else:
            bad.append(opt)
    if bad:
        LOG.warning('Ignoring the invalid elements of the option %(name)s: %(options)s', {'name': name, 'options': ', '.join(bad)})
    return good

@p.trace('validate_filter')
def validate_filter(filter):
    """Validates that the filter is configured in the default filters."""
    return filter in CONF.filter_scheduler.enabled_filters

@p.trace('validate_weigher')
def validate_weigher(weigher):
    """Validates that the weigher is configured in the default weighers."""
    weight_classes = CONF.filter_scheduler.weight_classes
    if 'nova.scheduler.weights.all_weighers' in weight_classes:
        return True
    return weigher in weight_classes
_SUPPORTS_AFFINITY = None
_SUPPORTS_ANTI_AFFINITY = None
_SUPPORTS_SOFT_AFFINITY = None
_SUPPORTS_SOFT_ANTI_AFFINITY = None

@p.trace('_get_group_details')
def _get_group_details(context, instance_uuid, user_group_hosts=None):
    """Provide group_hosts and group_policies sets related to instances if
    those instances are belonging to a group and if corresponding filters are
    enabled.

    :param instance_uuid: UUID of the instance to check
    :param user_group_hosts: Hosts from the group or empty set

    :returns: None or namedtuple GroupDetails
    """
    global _SUPPORTS_AFFINITY
    if _SUPPORTS_AFFINITY is None:
        _SUPPORTS_AFFINITY = validate_filter('ServerGroupAffinityFilter')
    global _SUPPORTS_ANTI_AFFINITY
    if _SUPPORTS_ANTI_AFFINITY is None:
        _SUPPORTS_ANTI_AFFINITY = validate_filter('ServerGroupAntiAffinityFilter')
    global _SUPPORTS_SOFT_AFFINITY
    if _SUPPORTS_SOFT_AFFINITY is None:
        _SUPPORTS_SOFT_AFFINITY = validate_weigher('nova.scheduler.weights.affinity.ServerGroupSoftAffinityWeigher')
    global _SUPPORTS_SOFT_ANTI_AFFINITY
    if _SUPPORTS_SOFT_ANTI_AFFINITY is None:
        _SUPPORTS_SOFT_ANTI_AFFINITY = validate_weigher('nova.scheduler.weights.affinity.ServerGroupSoftAntiAffinityWeigher')
    if not instance_uuid:
        return
    try:
        group = objects.InstanceGroup.get_by_instance_uuid(context, instance_uuid)
    except exception.InstanceGroupNotFound:
        return
    policies = set(('anti-affinity', 'affinity', 'soft-affinity', 'soft-anti-affinity'))
    if group.policy in policies:
        if not _SUPPORTS_AFFINITY and 'affinity' == group.policy:
            msg = _('ServerGroupAffinityFilter not configured')
            LOG.error(msg)
            raise exception.UnsupportedPolicyException(reason=msg)
        if not _SUPPORTS_ANTI_AFFINITY and 'anti-affinity' == group.policy:
            msg = _('ServerGroupAntiAffinityFilter not configured')
            LOG.error(msg)
            raise exception.UnsupportedPolicyException(reason=msg)
        if not _SUPPORTS_SOFT_AFFINITY and 'soft-affinity' == group.policy:
            msg = _('ServerGroupSoftAffinityWeigher not configured')
            LOG.error(msg)
            raise exception.UnsupportedPolicyException(reason=msg)
        if not _SUPPORTS_SOFT_ANTI_AFFINITY and 'soft-anti-affinity' == group.policy:
            msg = _('ServerGroupSoftAntiAffinityWeigher not configured')
            LOG.error(msg)
            raise exception.UnsupportedPolicyException(reason=msg)
        group_hosts = set(group.get_hosts())
        user_hosts = set(user_group_hosts) if user_group_hosts else set()
        return GroupDetails(hosts=user_hosts | group_hosts, policy=group.policy, members=group.members)

@p.trace('_get_instance_group_hosts_all_cells')
def _get_instance_group_hosts_all_cells(context, instance_group):

    def get_hosts_in_cell(cell_context):
        cell_instance_group = instance_group.obj_clone()
        with cell_instance_group.obj_alternate_context(cell_context):
            return cell_instance_group.get_hosts()
    results = nova_context.scatter_gather_skip_cell0(context, get_hosts_in_cell)
    hosts = []
    for result in results.values():
        if not nova_context.is_cell_failure_sentinel(result):
            hosts.extend(result)
    return hosts

@p.trace('setup_instance_group')
def setup_instance_group(context, request_spec):
    """Add group_hosts and group_policies fields to filter_properties dict
    based on instance uuids provided in request_spec, if those instances are
    belonging to a group.

    :param request_spec: Request spec
    """
    if request_spec.instance_group and 'hosts' not in request_spec.instance_group:
        group = request_spec.instance_group
        if context.db_connection:
            with group.obj_alternate_context(context):
                group.hosts = group.get_hosts()
        else:
            group.hosts = _get_instance_group_hosts_all_cells(context, group)
    if request_spec.instance_group and request_spec.instance_group.hosts:
        group_hosts = request_spec.instance_group.hosts
    else:
        group_hosts = None
    instance_uuid = request_spec.instance_uuid
    group_info = _get_group_details(context, instance_uuid, group_hosts)
    if group_info is not None:
        request_spec.instance_group.hosts = list(group_info.hosts)
        request_spec.instance_group.policy = group_info.policy
        request_spec.instance_group.members = group_info.members

@p.trace('request_is_rebuild')
def request_is_rebuild(spec_obj):
    """Returns True if request is for a rebuild.

    :param spec_obj: An objects.RequestSpec to examine (or None).
    """
    if not spec_obj:
        return False
    if 'scheduler_hints' not in spec_obj:
        return False
    check_type = spec_obj.scheduler_hints.get('_nova_check_type')
    return check_type == ['rebuild']

@p.trace('claim_resources')
def claim_resources(ctx, client, spec_obj, instance_uuid, alloc_req, allocation_request_version=None):
    """Given an instance UUID (representing the consumer of resources) and the
    allocation_request JSON object returned from Placement, attempt to claim
    resources for the instance in the placement API. Returns True if the claim
    process was successful, False otherwise.

    :param ctx: The RequestContext object
    :param client: The scheduler client to use for making the claim call
    :param spec_obj: The RequestSpec object - needed to get the project_id
    :param instance_uuid: The UUID of the consuming instance
    :param alloc_req: The allocation_request received from placement for the
                      resources we want to claim against the chosen host. The
                      allocation_request satisfies the original request for
                      resources and can be supplied as-is (along with the
                      project and user ID to the placement API's PUT
                      /allocations/{consumer_uuid} call to claim resources for
                      the instance
    :param allocation_request_version: The microversion used to request the
                                       allocations.
    """
    if request_is_rebuild(spec_obj):
        LOG.debug('Not claiming resources in the placement API for rebuild-only scheduling of instance %(uuid)s', {'uuid': instance_uuid})
        return True
    LOG.debug('Attempting to claim resources in the placement API for instance %s', instance_uuid)
    project_id = spec_obj.project_id
    if 'user_id' in spec_obj and spec_obj.user_id:
        user_id = spec_obj.user_id
    else:
        user_id = ctx.user_id
    return client.claim_resources(ctx, instance_uuid, alloc_req, project_id, user_id, allocation_request_version=allocation_request_version, consumer_generation=None)

@p.trace('get_weight_multiplier')
def get_weight_multiplier(host_state, multiplier_name, multiplier_config):
    """Given a HostState object, multplier_type name and multiplier_config,
    returns the weight multiplier.

    It reads the "multiplier_name" from "aggregate metadata" in host_state
    to override the multiplier_config. If the aggregate metadata doesn't
    contain the multiplier_name, the multiplier_config will be returned
    directly.

    :param host_state: The HostState object, which contains aggregate metadata
    :param multiplier_name: The weight multiplier name, like
           "cpu_weight_multiplier".
    :param multiplier_config: The weight multiplier configuration value
    """
    aggregate_vals = filters_utils.aggregate_values_from_key(host_state, multiplier_name)
    try:
        value = filters_utils.validate_num_values(aggregate_vals, multiplier_config, cast_to=float)
    except ValueError as e:
        LOG.warning("Could not decode '%(name)s' weight multiplier: %(exce)s", {'exce': e, 'name': multiplier_name})
        value = multiplier_config
    return value

@p.trace('fill_provider_mapping')
def fill_provider_mapping(request_spec, host_selection):
    """Fills out the request group - resource provider mapping in the
    request spec.

    :param request_spec: The RequestSpec object associated with the
        operation
    :param host_selection: The Selection object returned by the scheduler
        for this operation
    """
    if not request_spec.maps_requested_resources:
        return
    mappings = jsonutils.loads(host_selection.allocation_request)['mappings']
    for request_group in request_spec.requested_resources:
        request_group.provider_uuids = mappings[request_group.requester_id]

@p.trace('fill_provider_mapping_based_on_allocation')
def fill_provider_mapping_based_on_allocation(context, report_client, request_spec, allocation):
    """Fills out the request group - resource provider mapping in the
    request spec based on the current allocation of the instance.

    The fill_provider_mapping() variant is expected to be called in every
    scenario when a Selection object is available from the scheduler. However
    in case of revert operations such Selection does not exists. In this case
    the mapping is calculated based on the allocation of the source host the
    move operation is reverting to.

    This is a workaround as placement does not return which RP fulfills which
    granular request group except in the allocation candidate request (because
    request groups are ephemeral, only existing in the scope of that request).

    .. todo:: Figure out a better way to preserve the mappings so we can get
              rid of this workaround.

    :param context: The security context
    :param report_client: SchedulerReportClient instance to be used to
        communicate with placement
    :param request_spec: The RequestSpec object associated with the
        operation
    :param allocation: allocation dict of the instance, keyed by RP UUID.
    """
    if not request_spec.maps_requested_resources:
        return
    provider_traits = {rp_uuid: report_client.get_provider_traits(context, rp_uuid).traits for rp_uuid in allocation}
    request_spec.map_requested_resources_to_providers(allocation, provider_traits)

@p.trace('get_aggregates_for_routed_network')
def get_aggregates_for_routed_network(context, network_api, report_client, network_uuid):
    """Collects the aggregate UUIDs describing the segmentation of a routed
    network from Nova perspective.

    A routed network consists of multiple network segments. Each segment is
    available on a given set of compute hosts. Such segmentation is modelled as
    host aggregates from Nova perspective.

    :param context: The security context
    :param network_api: nova.network.neutron.API instance to be used to
       communicate with Neutron
    :param report_client: SchedulerReportClient instance to be used to
        communicate with Placement
    :param network_uuid: The UUID of the Neutron network to be translated to
        aggregates
    :returns: A list of aggregate UUIDs
    :raises InvalidRoutedNetworkConfiguration: if something goes wrong when
        try to find related aggregates
    """
    aggregates = []
    segment_ids = network_api.get_segment_ids_for_network(context, network_uuid)
    for segment_id in segment_ids:
        agg_info = report_client._get_provider_aggregates(context, segment_id)
        if agg_info is None or not agg_info.aggregates:
            raise exception.InvalidRoutedNetworkConfiguration('Failed to find aggregate related to segment %s' % segment_id)
        aggregates.extend(agg_info.aggregates)
    return aggregates

@p.trace('get_aggregates_for_routed_subnet')
def get_aggregates_for_routed_subnet(context, network_api, report_client, subnet_id):
    """Collects the aggregate UUIDs matching the segment that relates to a
    particular subnet from a routed network.

    A routed network consists of multiple network segments. Each segment is
    available on a given set of compute hosts. Such segmentation is modelled as
    host aggregates from Nova perspective.

    :param context: The security context
    :param network_api: nova.network.neutron.API instance to be used to
       communicate with Neutron
    :param report_client: SchedulerReportClient instance to be used to
        communicate with Placement
    :param subnet_id: The UUID of the Neutron subnet to be translated to
        aggregate
    :returns: A list of aggregate UUIDs
    :raises InvalidRoutedNetworkConfiguration: if something goes wrong when
        try to find related aggregates
    """
    segment_id = network_api.get_segment_id_for_subnet(context, subnet_id)
    if segment_id:
        agg_info = report_client._get_provider_aggregates(context, segment_id)
        if agg_info is None or not agg_info.aggregates:
            raise exception.InvalidRoutedNetworkConfiguration('Failed to find aggregate related to segment %s' % segment_id)
        return agg_info.aggregates
    return []