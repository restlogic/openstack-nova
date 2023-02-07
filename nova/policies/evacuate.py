from bees import profiler as p
from oslo_policy import policy
from nova.policies import base
BASE_POLICY_NAME = 'os_compute_api:os-evacuate'
evacuate_policies = [policy.DocumentedRuleDefault(name=BASE_POLICY_NAME, check_str=base.SYSTEM_ADMIN, description='Evacuate a server from a failed host to a new host', operations=[{'path': '/servers/{server_id}/action (evacuate)', 'method': 'POST'}], scope_types=['system', 'project'])]

@p.trace('list_rules')
def list_rules():
    return evacuate_policies