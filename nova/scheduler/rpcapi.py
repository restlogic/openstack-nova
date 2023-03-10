from bees import profiler as p
'\nClient side of the scheduler manager RPC API.\n'
import oslo_messaging as messaging
import nova.conf
from nova import exception as exc
from nova.objects import base as objects_base
from nova import profiler
from nova import rpc
CONF = nova.conf.CONF
RPC_TOPIC = 'scheduler'

@p.trace_cls('SchedulerAPI')
@profiler.trace_cls('rpc')
class SchedulerAPI(object):
    """Client side of the scheduler rpc API.

    API version history:

        * 1.0 - Initial version.
        * 1.1 - Changes to prep_resize():
            * remove instance_uuid, add instance
            * remove instance_type_id, add instance_type
            * remove topic, it was unused
        * 1.2 - Remove topic from run_instance, it was unused
        * 1.3 - Remove instance_id, add instance to live_migration
        * 1.4 - Remove update_db from prep_resize
        * 1.5 - Add reservations argument to prep_resize()
        * 1.6 - Remove reservations argument to run_instance()
        * 1.7 - Add create_volume() method, remove topic from live_migration()

        * 2.0 - Remove 1.x backwards compat
        * 2.1 - Add image_id to create_volume()
        * 2.2 - Remove reservations argument to create_volume()
        * 2.3 - Remove create_volume()
        * 2.4 - Change update_service_capabilities()
            * accepts a list of capabilities
        * 2.5 - Add get_backdoor_port()
        * 2.6 - Add select_hosts()

        ... Grizzly supports message version 2.6.  So, any changes to existing
        methods in 2.x after that point should be done such that they can
        handle the version_cap being set to 2.6.

        * 2.7 - Add select_destinations()
        * 2.8 - Deprecate prep_resize() -- JUST KIDDING.  It is still used
                by the compute manager for retries.
        * 2.9 - Added the legacy_bdm_in_spec parameter to run_instance()

        ... Havana supports message version 2.9.  So, any changes to existing
        methods in 2.x after that point should be done such that they can
        handle the version_cap being set to 2.9.

        * Deprecated live_migration() call, moved to conductor
        * Deprecated select_hosts()

        3.0 - Removed backwards compat

        ... Icehouse and Juno support message version 3.0.  So, any changes to
        existing methods in 3.x after that point should be done such that they
        can handle the version_cap being set to 3.0.

        * 3.1 - Made select_destinations() send flavor object

        * 4.0 - Removed backwards compat for Icehouse
        * 4.1 - Add update_aggregates() and delete_aggregate()
        * 4.2 - Added update_instance_info(), delete_instance_info(), and
                sync_instance_info()  methods

        ... Kilo and Liberty support message version 4.2. So, any
        changes to existing methods in 4.x after that point should be
        done such that they can handle the version_cap being set to
        4.2.

        * 4.3 - Modify select_destinations() signature by providing a
                RequestSpec obj

        ... Mitaka, Newton, and Ocata support message version 4.3. So, any
        changes to existing methods in 4.x after that point should be done such
        that they can handle the version_cap being set to 4.3.

        * 4.4 - Modify select_destinations() signature by providing the
                instance_uuids for the request.

        ... Pike supports message version 4.4. So any changes to existing
        methods in 4.x after that point should be done such
        that they can handle the version_cap being set to 4.4.

        * 4.5 - Modify select_destinations() to optionally return a list of
                lists of Selection objects, along with zero or more alternates.
    """
    VERSION_ALIASES = {'grizzly': '2.6', 'havana': '2.9', 'icehouse': '3.0', 'juno': '3.0', 'kilo': '4.2', 'liberty': '4.2', 'mitaka': '4.3', 'newton': '4.3', 'ocata': '4.3', 'pike': '4.4'}

    def __init__(self):
        super(SchedulerAPI, self).__init__()
        target = messaging.Target(topic=RPC_TOPIC, version='4.0')
        version_cap = self.VERSION_ALIASES.get(CONF.upgrade_levels.scheduler, CONF.upgrade_levels.scheduler)
        serializer = objects_base.NovaObjectSerializer()
        self.client = rpc.get_client(target, version_cap=version_cap, serializer=serializer)

    def select_destinations(self, ctxt, spec_obj, instance_uuids, return_objects=False, return_alternates=False):
        version = '4.5'
        msg_args = {'instance_uuids': instance_uuids, 'spec_obj': spec_obj, 'return_objects': return_objects, 'return_alternates': return_alternates}
        if not self.client.can_send_version(version):
            if msg_args['return_objects'] or msg_args['return_alternates']:
                raise exc.SelectionObjectsWithOldRPCVersionNotSupported(version=self.client.version_cap)
            del msg_args['return_objects']
            del msg_args['return_alternates']
            version = '4.4'
        if not self.client.can_send_version(version):
            del msg_args['instance_uuids']
            version = '4.3'
        if not self.client.can_send_version(version):
            del msg_args['spec_obj']
            msg_args['request_spec'] = spec_obj.to_legacy_request_spec_dict()
            msg_args['filter_properties'] = spec_obj.to_legacy_filter_properties_dict()
            version = '4.0'
        cctxt = self.client.prepare(version=version, call_monitor_timeout=CONF.rpc_response_timeout, timeout=CONF.long_rpc_timeout)
        return cctxt.call(ctxt, 'select_destinations', **msg_args)

    def update_aggregates(self, ctxt, aggregates):
        cctxt = self.client.prepare(fanout=True, version='4.1')
        cctxt.cast(ctxt, 'update_aggregates', aggregates=aggregates)

    def delete_aggregate(self, ctxt, aggregate):
        cctxt = self.client.prepare(fanout=True, version='4.1')
        cctxt.cast(ctxt, 'delete_aggregate', aggregate=aggregate)

    def update_instance_info(self, ctxt, host_name, instance_info):
        cctxt = self.client.prepare(version='4.2', fanout=True)
        return cctxt.cast(ctxt, 'update_instance_info', host_name=host_name, instance_info=instance_info)

    def delete_instance_info(self, ctxt, host_name, instance_uuid):
        cctxt = self.client.prepare(version='4.2', fanout=True)
        return cctxt.cast(ctxt, 'delete_instance_info', host_name=host_name, instance_uuid=instance_uuid)

    def sync_instance_info(self, ctxt, host_name, instance_uuids):
        cctxt = self.client.prepare(version='4.2', fanout=True)
        return cctxt.cast(ctxt, 'sync_instance_info', host_name=host_name, instance_uuids=instance_uuids)