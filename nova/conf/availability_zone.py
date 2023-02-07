from bees import profiler as p
from oslo_config import cfg
availability_zone_opts = [cfg.StrOpt('internal_service_availability_zone', default='internal', help="\nAvailability zone for internal services.\n\nThis option determines the availability zone for the various internal nova\nservices, such as 'nova-scheduler', 'nova-conductor', etc.\n\nPossible values:\n\n* Any string representing an existing availability zone name.\n"), cfg.StrOpt('default_availability_zone', default='nova', help="\nDefault availability zone for compute services.\n\nThis option determines the default availability zone for 'nova-compute'\nservices, which will be used if the service(s) do not belong to aggregates with\navailability zone metadata.\n\nPossible values:\n\n* Any string representing an existing availability zone name.\n"), cfg.StrOpt('default_schedule_zone', help='\nDefault availability zone for instances.\n\nThis option determines the default availability zone for instances, which will\nbe used when a user does not specify one when creating an instance. The\ninstance(s) will be bound to this availability zone for their lifetime.\n\nPossible values:\n\n* Any string representing an existing availability zone name.\n* None, which means that the instance can move from one availability zone to\n  another during its lifetime if it is moved from one compute node to another.\n\nRelated options:\n\n* ``[cinder]/cross_az_attach``\n')]

@p.trace('register_opts')
def register_opts(conf):
    conf.register_opts(availability_zone_opts)

@p.trace('list_opts')
def list_opts():
    return {'DEFAULT': availability_zone_opts}