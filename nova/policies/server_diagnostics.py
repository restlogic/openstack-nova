from bees import profiler as p
from oslo_policy import policy
from nova.policies import base
BASE_POLICY_NAME = 'os_compute_api:os-server-diagnostics'
server_diagnostics_policies = [policy.DocumentedRuleDefault(name=BASE_POLICY_NAME, check_str=base.SYSTEM_ADMIN, description='Show the usage data for a server', operations=[{'method': 'GET', 'path': '/servers/{server_id}/diagnostics'}], scope_types=['system', 'project'])]

@p.trace('list_rules')
def list_rules():
    return server_diagnostics_policies