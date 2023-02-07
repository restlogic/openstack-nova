from bees import profiler as p
from oslo_policy import policy
from nova.policies import base
BASE_POLICY_NAME = 'os_compute_api:os-remote-consoles'
remote_consoles_policies = [policy.DocumentedRuleDefault(name=BASE_POLICY_NAME, check_str=base.PROJECT_MEMBER_OR_SYSTEM_ADMIN, description='Generate a URL to access remove server console.\n\nThis policy is for ``POST /remote-consoles`` API and below Server actions APIs\nare deprecated:\n\n- ``os-getRDPConsole``\n- ``os-getSerialConsole``\n- ``os-getSPICEConsole``\n- ``os-getVNCConsole``.', operations=[{'method': 'POST', 'path': '/servers/{server_id}/action (os-getRDPConsole)'}, {'method': 'POST', 'path': '/servers/{server_id}/action (os-getSerialConsole)'}, {'method': 'POST', 'path': '/servers/{server_id}/action (os-getSPICEConsole)'}, {'method': 'POST', 'path': '/servers/{server_id}/action (os-getVNCConsole)'}, {'method': 'POST', 'path': '/servers/{server_id}/remote-consoles'}], scope_types=['system', 'project'])]

@p.trace('list_rules')
def list_rules():
    return remote_consoles_policies