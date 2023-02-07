from bees import profiler as p
import contextlib

@p.trace_cls('VirtAPI')
class VirtAPI(object):

    @contextlib.contextmanager
    def wait_for_instance_event(self, instance, event_names, deadline=300, error_callback=None):
        raise NotImplementedError()

    def exit_wait_early(self, events):
        raise NotImplementedError()

    def update_compute_provider_status(self, context, rp_uuid, enabled):
        """Used to add/remove the COMPUTE_STATUS_DISABLED trait on the provider

        :param context: nova auth RequestContext
        :param rp_uuid: UUID of a compute node resource provider in Placement
        :param enabled: True if the node is enabled in which case the trait
            would be removed, False if the node is disabled in which case
            the trait would be added.
        """
        raise NotImplementedError()