from bees import profiler as p
'\nThe FilterScheduler is for creating instances locally.\nYou can customize this scheduler by specifying your own Host Filters and\nWeighing Functions.\n'
import random
from oslo_log import log as logging
from nova.compute import utils as compute_utils
import nova.conf
from nova import exception
from nova.i18n import _
from nova import objects
from nova.objects import fields as fields_obj
from nova import rpc
from nova.scheduler.client import report
from nova.scheduler import driver
from nova.scheduler import utils
CONF = nova.conf.CONF
LOG = logging.getLogger(__name__)

@p.trace_cls('FilterScheduler')
class FilterScheduler(driver.Scheduler):
    """Scheduler that can be used for filtering and weighing."""

    def __init__(self, *args, **kwargs):
        super(FilterScheduler, self).__init__(*args, **kwargs)
        self.notifier = rpc.get_notifier('scheduler')
        self.placement_client = report.SchedulerReportClient()

    def select_destinations(self, context, spec_obj, instance_uuids, alloc_reqs_by_rp_uuid, provider_summaries, allocation_request_version=None, return_alternates=False):
        """Returns a list of lists of Selection objects, which represent the
        hosts and (optionally) alternates for each instance.

        :param context: The RequestContext object
        :param spec_obj: The RequestSpec object
        :param instance_uuids: List of UUIDs, one for each value of the spec
                               object's num_instances attribute
        :param alloc_reqs_by_rp_uuid: Optional dict, keyed by resource provider
                                      UUID, of the allocation_requests that may
                                      be used to claim resources against
                                      matched hosts. If None, indicates either
                                      the placement API wasn't reachable or
                                      that there were no allocation_requests
                                      returned by the placement API. If the
                                      latter, the provider_summaries will be an
                                      empty dict, not None.
        :param provider_summaries: Optional dict, keyed by resource provider
                                   UUID, of information that will be used by
                                   the filters/weighers in selecting matching
                                   hosts for a request. If None, indicates that
                                   the scheduler driver should grab all compute
                                   node information locally and that the
                                   Placement API is not used. If an empty dict,
                                   indicates the Placement API returned no
                                   potential matches for the requested
                                   resources.
        :param allocation_request_version: The microversion used to request the
                                           allocations.
        :param return_alternates: When True, zero or more alternate hosts are
                                  returned with each selected host. The number
                                  of alternates is determined by the
                                  configuration option
                                  `CONF.scheduler.max_attempts`.
        """
        self.notifier.info(context, 'scheduler.select_destinations.start', dict(request_spec=spec_obj.to_legacy_request_spec_dict()))
        compute_utils.notify_about_scheduler_action(context=context, request_spec=spec_obj, action=fields_obj.NotificationAction.SELECT_DESTINATIONS, phase=fields_obj.NotificationPhase.START)
        host_selections = self._schedule(context, spec_obj, instance_uuids, alloc_reqs_by_rp_uuid, provider_summaries, allocation_request_version, return_alternates)
        self.notifier.info(context, 'scheduler.select_destinations.end', dict(request_spec=spec_obj.to_legacy_request_spec_dict()))
        compute_utils.notify_about_scheduler_action(context=context, request_spec=spec_obj, action=fields_obj.NotificationAction.SELECT_DESTINATIONS, phase=fields_obj.NotificationPhase.END)
        return host_selections

    def _schedule(self, context, spec_obj, instance_uuids, alloc_reqs_by_rp_uuid, provider_summaries, allocation_request_version=None, return_alternates=False):
        """Returns a list of lists of Selection objects.

        :param context: The RequestContext object
        :param spec_obj: The RequestSpec object
        :param instance_uuids: List of instance UUIDs to place or move.
        :param alloc_reqs_by_rp_uuid: Optional dict, keyed by resource provider
                                      UUID, of the allocation_requests that may
                                      be used to claim resources against
                                      matched hosts. If None, indicates either
                                      the placement API wasn't reachable or
                                      that there were no allocation_requests
                                      returned by the placement API. If the
                                      latter, the provider_summaries will be an
                                      empty dict, not None.
        :param provider_summaries: Optional dict, keyed by resource provider
                                   UUID, of information that will be used by
                                   the filters/weighers in selecting matching
                                   hosts for a request. If None, indicates that
                                   the scheduler driver should grab all compute
                                   node information locally and that the
                                   Placement API is not used. If an empty dict,
                                   indicates the Placement API returned no
                                   potential matches for the requested
                                   resources.
        :param allocation_request_version: The microversion used to request the
                                           allocations.
        :param return_alternates: When True, zero or more alternate hosts are
                                  returned with each selected host. The number
                                  of alternates is determined by the
                                  configuration option
                                  `CONF.scheduler.max_attempts`.
        """
        elevated = context.elevated()
        hosts = self._get_all_host_states(elevated, spec_obj, provider_summaries)
        num_instances = len(instance_uuids) if instance_uuids else spec_obj.num_instances
        num_alts = CONF.scheduler.max_attempts - 1 if return_alternates else 0
        if instance_uuids is None or not self.USES_ALLOCATION_CANDIDATES or alloc_reqs_by_rp_uuid is None:
            return self._legacy_find_hosts(context, num_instances, spec_obj, hosts, num_alts, instance_uuids=instance_uuids)
        claimed_instance_uuids = []
        claimed_hosts = []
        for (num, instance_uuid) in enumerate(instance_uuids):
            spec_obj.instance_uuid = instance_uuid
            spec_obj.obj_reset_changes(['instance_uuid'])
            hosts = self._get_sorted_hosts(spec_obj, hosts, num)
            if not hosts:
                break
            claimed_host = None
            for host in hosts:
                cn_uuid = host.uuid
                if cn_uuid not in alloc_reqs_by_rp_uuid:
                    msg = "A host state with uuid = '%s' that did not have a matching allocation_request was encountered while scheduling. This host was skipped."
                    LOG.debug(msg, cn_uuid)
                    continue
                alloc_reqs = alloc_reqs_by_rp_uuid[cn_uuid]
                alloc_req = alloc_reqs[0]
                if utils.claim_resources(elevated, self.placement_client, spec_obj, instance_uuid, alloc_req, allocation_request_version=allocation_request_version):
                    claimed_host = host
                    break
            if claimed_host is None:
                LOG.debug('Unable to successfully claim against any host.')
                break
            claimed_instance_uuids.append(instance_uuid)
            claimed_hosts.append(claimed_host)
            self._consume_selected_host(claimed_host, spec_obj, instance_uuid=instance_uuid)
        self._ensure_sufficient_hosts(context, claimed_hosts, num_instances, claimed_instance_uuids)
        selections_to_return = self._get_alternate_hosts(claimed_hosts, spec_obj, hosts, num, num_alts, alloc_reqs_by_rp_uuid, allocation_request_version)
        return selections_to_return

    def _ensure_sufficient_hosts(self, context, hosts, required_count, claimed_uuids=None):
        """Checks that we have selected a host for each requested instance. If
        not, log this failure, remove allocations for any claimed instances,
        and raise a NoValidHost exception.
        """
        if len(hosts) == required_count:
            return
        if claimed_uuids:
            self._cleanup_allocations(context, claimed_uuids)
        for host in hosts:
            host.updated = None
        LOG.debug('There are %(hosts)d hosts available but %(required_count)d instances requested to build.', {'hosts': len(hosts), 'required_count': required_count})
        reason = _('There are not enough hosts available.')
        raise exception.NoValidHost(reason=reason)

    def _cleanup_allocations(self, context, instance_uuids):
        """Removes allocations for the supplied instance UUIDs."""
        if not instance_uuids:
            return
        LOG.debug('Cleaning up allocations for %s', instance_uuids)
        for uuid in instance_uuids:
            self.placement_client.delete_allocation_for_instance(context, uuid)

    def _legacy_find_hosts(self, context, num_instances, spec_obj, hosts, num_alts, instance_uuids=None):
        """Some schedulers do not do claiming, or we can sometimes not be able
        to if the Placement service is not reachable. Additionally, we may be
        working with older conductors that don't pass in instance_uuids.
        """
        selected_hosts = []
        for num in range(num_instances):
            instance_uuid = instance_uuids[num] if instance_uuids else None
            if instance_uuid:
                spec_obj.instance_uuid = instance_uuid
                spec_obj.obj_reset_changes(['instance_uuid'])
            hosts = self._get_sorted_hosts(spec_obj, hosts, num)
            if not hosts:
                break
            selected_host = hosts[0]
            selected_hosts.append(selected_host)
            self._consume_selected_host(selected_host, spec_obj, instance_uuid=instance_uuid)
        self._ensure_sufficient_hosts(context, selected_hosts, num_instances)
        selections_to_return = self._get_alternate_hosts(selected_hosts, spec_obj, hosts, num, num_alts)
        return selections_to_return

    @staticmethod
    def _consume_selected_host(selected_host, spec_obj, instance_uuid=None):
        LOG.debug('Selected host: %(host)s', {'host': selected_host}, instance_uuid=instance_uuid)
        selected_host.consume_from_request(spec_obj)
        if spec_obj.instance_group is not None:
            spec_obj.instance_group.hosts.append(selected_host.host)
            spec_obj.instance_group.obj_reset_changes(['hosts'])
            if instance_uuid and instance_uuid not in selected_host.instances:
                selected_host.instances[instance_uuid] = objects.Instance(uuid=instance_uuid)

    def _get_alternate_hosts(self, selected_hosts, spec_obj, hosts, index, num_alts, alloc_reqs_by_rp_uuid=None, allocation_request_version=None):
        if index > 0 and num_alts > 0:
            hosts = self._get_sorted_hosts(spec_obj, hosts, index)
        selections_to_return = []
        for selected_host in selected_hosts:
            if alloc_reqs_by_rp_uuid:
                selected_alloc_req = alloc_reqs_by_rp_uuid.get(selected_host.uuid)[0]
            else:
                selected_alloc_req = None
            selection = objects.Selection.from_host_state(selected_host, allocation_request=selected_alloc_req, allocation_request_version=allocation_request_version)
            selected_plus_alts = [selection]
            cell_uuid = selected_host.cell_uuid
            for host in hosts:
                if len(selected_plus_alts) >= num_alts + 1:
                    break
                if host.cell_uuid == cell_uuid and host not in selected_hosts:
                    if alloc_reqs_by_rp_uuid is not None:
                        alt_uuid = host.uuid
                        if alt_uuid not in alloc_reqs_by_rp_uuid:
                            msg = "A host state with uuid = '%s' that did not have a matching allocation_request was encountered while scheduling. This host was skipped."
                            LOG.debug(msg, alt_uuid)
                            continue
                        alloc_req = alloc_reqs_by_rp_uuid[alt_uuid][0]
                        alt_selection = objects.Selection.from_host_state(host, alloc_req, allocation_request_version)
                    else:
                        alt_selection = objects.Selection.from_host_state(host)
                    selected_plus_alts.append(alt_selection)
            selections_to_return.append(selected_plus_alts)
        return selections_to_return

    def _get_sorted_hosts(self, spec_obj, host_states, index):
        """Returns a list of HostState objects that match the required
        scheduling constraints for the request spec object and have been sorted
        according to the weighers.
        """
        filtered_hosts = self.host_manager.get_filtered_hosts(host_states, spec_obj, index)
        LOG.debug('Filtered %(hosts)s', {'hosts': filtered_hosts})
        if not filtered_hosts:
            return []
        weighed_hosts = self.host_manager.get_weighed_hosts(filtered_hosts, spec_obj)
        if CONF.filter_scheduler.shuffle_best_same_weighed_hosts:
            best_hosts = [w for w in weighed_hosts if w.weight == weighed_hosts[0].weight]
            random.shuffle(best_hosts)
            weighed_hosts = best_hosts + weighed_hosts[len(best_hosts):]
        LOG.debug('Weighed %(hosts)s', {'hosts': weighed_hosts})
        weighed_hosts = [h.obj for h in weighed_hosts]
        host_subset_size = CONF.filter_scheduler.host_subset_size
        if host_subset_size < len(weighed_hosts):
            weighed_subset = weighed_hosts[0:host_subset_size]
        else:
            weighed_subset = weighed_hosts
        chosen_host = random.choice(weighed_subset)
        weighed_hosts.remove(chosen_host)
        return [chosen_host] + weighed_hosts

    def _get_all_host_states(self, context, spec_obj, provider_summaries):
        """Template method, so a subclass can implement caching."""
        compute_uuids = None
        if provider_summaries is not None:
            compute_uuids = list(provider_summaries.keys())
        return self.host_manager.get_host_states_by_uuids(context, compute_uuids, spec_obj)