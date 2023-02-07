from bees import profiler as p
from oslo_policy import policy
from nova.policies import base
BASE_POLICY_NAME = 'os_compute_api:os-extended-server-attributes'
extended_server_attributes_policies = [policy.DocumentedRuleDefault(name=BASE_POLICY_NAME, check_str=base.SYSTEM_ADMIN, description='Return extended attributes for server.\n\nThis rule will control the visibility for a set of servers attributes:\n\n- ``OS-EXT-SRV-ATTR:host``\n- ``OS-EXT-SRV-ATTR:instance_name``\n- ``OS-EXT-SRV-ATTR:reservation_id`` (since microversion 2.3)\n- ``OS-EXT-SRV-ATTR:launch_index`` (since microversion 2.3)\n- ``OS-EXT-SRV-ATTR:hostname`` (since microversion 2.3)\n- ``OS-EXT-SRV-ATTR:kernel_id`` (since microversion 2.3)\n- ``OS-EXT-SRV-ATTR:ramdisk_id`` (since microversion 2.3)\n- ``OS-EXT-SRV-ATTR:root_device_name`` (since microversion 2.3)\n- ``OS-EXT-SRV-ATTR:user_data`` (since microversion 2.3)\n\nMicrovision 2.75 added the above attributes in the ``PUT /servers/{server_id}``\nand ``POST /servers/{server_id}/action (rebuild)`` API responses which are\nalso controlled by this policy rule, like the ``GET /servers*`` APIs.\n', operations=[{'method': 'GET', 'path': '/servers/{id}'}, {'method': 'GET', 'path': '/servers/detail'}, {'method': 'PUT', 'path': '/servers/{server_id}'}, {'method': 'POST', 'path': '/servers/{server_id}/action (rebuild)'}], scope_types=['system', 'project'])]

@p.trace('list_rules')
def list_rules():
    return extended_server_attributes_policies