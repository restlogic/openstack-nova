from bees import profiler as p
'\nScheduler base class that all Schedulers should inherit from\n'
import abc
from nova import objects
from nova.scheduler import host_manager
from nova import servicegroup

@p.trace_cls('Scheduler')
class Scheduler(metaclass=abc.ABCMeta):
    """The base class that all Scheduler classes should inherit from."""
    USES_ALLOCATION_CANDIDATES = True
    'Indicates that the scheduler driver calls the Placement API for\n    allocation candidates and uses those allocation candidates in its\n    decision-making.\n    '

    def __init__(self):
        self.host_manager = host_manager.HostManager()
        self.servicegroup_api = servicegroup.API()

    def hosts_up(self, context, topic):
        """Return the list of hosts that have a running service for topic."""
        services = objects.ServiceList.get_by_topic(context, topic)
        return [service.host for service in services if self.servicegroup_api.service_is_up(service)]

    @abc.abstractmethod
    def select_destinations(self, context, spec_obj, instance_uuids, alloc_reqs_by_rp_uuid, provider_summaries, allocation_request_version=None, return_alternates=False):
        """Returns a list of lists of Selection objects that have been chosen
        by the scheduler driver, one for each requested instance.
        """
        return []