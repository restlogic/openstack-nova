from bees import profiler as p
'Functionality related to notifications common to multiple layers of\nthe system.\n'
import datetime
from keystoneauth1 import exceptions as ks_exc
from oslo_log import log
from oslo_utils import excutils
from oslo_utils import timeutils
import nova.conf
import nova.context
from nova import exception
from nova.image import glance
from nova.notifications.objects import base as notification_base
from nova.notifications.objects import instance as instance_notification
from nova.objects import fields
from nova import rpc
from nova import utils
LOG = log.getLogger(__name__)
CONF = nova.conf.CONF

@p.trace('send_update')
def send_update(context, old_instance, new_instance, service='compute', host=None):
    """Send compute.instance.update notification to report any changes occurred
    in that instance
    """
    if not CONF.notifications.notify_on_state_change:
        return
    update_with_state_change = False
    old_vm_state = old_instance['vm_state']
    new_vm_state = new_instance['vm_state']
    old_task_state = old_instance['task_state']
    new_task_state = new_instance['task_state']
    if old_vm_state != new_vm_state:
        update_with_state_change = True
    elif CONF.notifications.notify_on_state_change == 'vm_and_task_state' and old_task_state != new_task_state:
        update_with_state_change = True
    if update_with_state_change:
        send_update_with_states(context, new_instance, old_vm_state, new_vm_state, old_task_state, new_task_state, service, host)
    else:
        try:
            old_display_name = None
            if new_instance['display_name'] != old_instance['display_name']:
                old_display_name = old_instance['display_name']
            send_instance_update_notification(context, new_instance, service=service, host=host, old_display_name=old_display_name)
        except exception.InstanceNotFound:
            LOG.debug('Failed to send instance update notification. The instance could not be found and was most likely deleted.', instance=new_instance)
        except Exception:
            LOG.exception('Failed to send state update notification', instance=new_instance)

@p.trace('send_update_with_states')
def send_update_with_states(context, instance, old_vm_state, new_vm_state, old_task_state, new_task_state, service='compute', host=None, verify_states=False):
    """Send compute.instance.update notification to report changes if there
    are any, in the instance
    """
    if not CONF.notifications.notify_on_state_change:
        return
    fire_update = True
    if verify_states:
        fire_update = False
        if old_vm_state != new_vm_state:
            fire_update = True
        elif CONF.notifications.notify_on_state_change == 'vm_and_task_state' and old_task_state != new_task_state:
            fire_update = True
    if fire_update:
        try:
            send_instance_update_notification(context, instance, old_vm_state=old_vm_state, old_task_state=old_task_state, new_vm_state=new_vm_state, new_task_state=new_task_state, service=service, host=host)
        except exception.InstanceNotFound:
            LOG.debug('Failed to send instance update notification. The instance could not be found and was most likely deleted.', instance=instance)
        except Exception:
            LOG.exception('Failed to send state update notification', instance=instance)

@p.trace('_compute_states_payload')
def _compute_states_payload(instance, old_vm_state=None, old_task_state=None, new_vm_state=None, new_task_state=None):
    if new_vm_state is None:
        new_vm_state = instance['vm_state']
    if new_task_state is None:
        new_task_state = instance['task_state']
    if old_vm_state is None:
        old_vm_state = instance['vm_state']
    if old_task_state is None:
        old_task_state = instance['task_state']
    states_payload = {'old_state': old_vm_state, 'state': new_vm_state, 'old_task_state': old_task_state, 'new_task_state': new_task_state}
    return states_payload

@p.trace('send_instance_update_notification')
def send_instance_update_notification(context, instance, old_vm_state=None, old_task_state=None, new_vm_state=None, new_task_state=None, service='compute', host=None, old_display_name=None):
    """Send 'compute.instance.update' notification to inform observers
    about instance state changes.
    """
    populate_image_ref_url = CONF.notifications.notification_format in ('both', 'unversioned')
    payload = info_from_instance(context, instance, None, populate_image_ref_url=populate_image_ref_url)
    payload.update(_compute_states_payload(instance, old_vm_state, old_task_state, new_vm_state, new_task_state))
    (audit_start, audit_end) = audit_period_bounds(current_period=True)
    payload['audit_period_beginning'] = null_safe_isotime(audit_start)
    payload['audit_period_ending'] = null_safe_isotime(audit_end)
    payload['bandwidth'] = {}
    if old_display_name:
        payload['old_display_name'] = old_display_name
    rpc.get_notifier(service, host).info(context, 'compute.instance.update', payload)
    _send_versioned_instance_update(context, instance, payload, host, service)

@p.trace('_send_versioned_instance_update')
@rpc.if_notifications_enabled
def _send_versioned_instance_update(context, instance, payload, host, service):

    def _map_legacy_service_to_source(legacy_service):
        if not legacy_service.startswith('nova-'):
            return 'nova-' + service
        else:
            return service
    state_update = instance_notification.InstanceStateUpdatePayload(old_state=payload.get('old_state'), state=payload.get('state'), old_task_state=payload.get('old_task_state'), new_task_state=payload.get('new_task_state'))
    audit_period = instance_notification.AuditPeriodPayload(audit_period_beginning=payload.get('audit_period_beginning'), audit_period_ending=payload.get('audit_period_ending'))
    bandwidth = []
    versioned_payload = instance_notification.InstanceUpdatePayload(context=context, instance=instance, state_update=state_update, audit_period=audit_period, bandwidth=bandwidth, old_display_name=payload.get('old_display_name'))
    notification = instance_notification.InstanceUpdateNotification(priority=fields.NotificationPriority.INFO, event_type=notification_base.EventType(object='instance', action=fields.NotificationAction.UPDATE), publisher=notification_base.NotificationPublisher(host=host or CONF.host, source=_map_legacy_service_to_source(service)), payload=versioned_payload)
    notification.emit(context)

@p.trace('audit_period_bounds')
def audit_period_bounds(current_period=False):
    """Get the start and end of the relevant audit usage period

    :param current_period: if True, this will generate a usage for the
        current usage period; if False, this will generate a usage for the
        previous audit period.
    """
    (begin, end) = utils.last_completed_audit_period()
    if current_period:
        audit_start = end
        audit_end = timeutils.utcnow()
    else:
        audit_start = begin
        audit_end = end
    return (audit_start, audit_end)

@p.trace('image_meta')
def image_meta(system_metadata):
    """Format image metadata for use in notifications from the instance
    system metadata.
    """
    image_meta = {}
    for (md_key, md_value) in system_metadata.items():
        if md_key.startswith('image_'):
            image_meta[md_key[6:]] = md_value
    return image_meta

@p.trace('null_safe_str')
def null_safe_str(s):
    return str(s) if s else ''

@p.trace('null_safe_isotime')
def null_safe_isotime(s):
    if isinstance(s, datetime.datetime):
        return utils.strtime(s)
    else:
        return str(s) if s else ''

@p.trace('info_from_instance')
def info_from_instance(context, instance, network_info, populate_image_ref_url=False, **kw):
    """Get detailed instance information for an instance which is common to all
    notifications.

    :param:instance: nova.objects.Instance
    :param:network_info: network_info provided if not None
    :param:populate_image_ref_url: If True then the full URL of the image of
                                   the instance is generated and returned.
                                   This, depending on the configuration, might
                                   mean a call to Keystone. If false, None
                                   value is returned in the dict at the
                                   image_ref_url key.
    """
    image_ref_url = None
    if populate_image_ref_url:
        try:
            image_ref_url = glance.API().generate_image_url(instance.image_ref, context)
        except ks_exc.EndpointNotFound:
            with excutils.save_and_reraise_exception() as exc_ctx:
                if context.auth_token is None:
                    image_ref_url = instance.image_ref
                    exc_ctx.reraise = False
    instance_type = instance.get_flavor()
    instance_type_name = instance_type.get('name', '')
    instance_flavorid = instance_type.get('flavorid', '')
    instance_info = dict(tenant_id=instance.project_id, user_id=instance.user_id, instance_id=instance.uuid, display_name=instance.display_name, reservation_id=instance.reservation_id, hostname=instance.hostname, instance_type=instance_type_name, instance_type_id=instance.instance_type_id, instance_flavor_id=instance_flavorid, architecture=instance.architecture, memory_mb=instance.flavor.memory_mb, disk_gb=instance.flavor.root_gb + instance.flavor.ephemeral_gb, vcpus=instance.flavor.vcpus, root_gb=instance.flavor.root_gb, ephemeral_gb=instance.flavor.ephemeral_gb, host=instance.host, node=instance.node, availability_zone=instance.availability_zone, cell_name=null_safe_str(instance.cell_name), created_at=str(instance.created_at), terminated_at=null_safe_isotime(instance.get('terminated_at', None)), deleted_at=null_safe_isotime(instance.get('deleted_at', None)), launched_at=null_safe_isotime(instance.get('launched_at', None)), image_ref_url=image_ref_url, os_type=instance.os_type, kernel_id=instance.kernel_id, ramdisk_id=instance.ramdisk_id, state=instance.vm_state, state_description=null_safe_str(instance.task_state), progress=int(instance.progress) if instance.progress else '', access_ip_v4=instance.access_ip_v4, access_ip_v6=instance.access_ip_v6)
    if network_info is not None:
        fixed_ips = []
        for vif in network_info:
            for ip in vif.fixed_ips():
                ip['label'] = vif['network']['label']
                ip['vif_mac'] = vif['address']
                fixed_ips.append(ip)
        instance_info['fixed_ips'] = fixed_ips
    image_meta_props = image_meta(instance.system_metadata)
    instance_info['image_meta'] = image_meta_props
    instance_info['metadata'] = instance.metadata
    instance_info.update(kw)
    return instance_info