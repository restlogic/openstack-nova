from bees import profiler as p
from oslo_policy import policy
from nova.policies import base
POLICY_ROOT = 'os_compute_api:ips:%s'
ips_policies = [policy.DocumentedRuleDefault(name=POLICY_ROOT % 'show', check_str=base.PROJECT_READER_OR_SYSTEM_READER, description='Show IP addresses details for a network label of a  server', operations=[{'method': 'GET', 'path': '/servers/{server_id}/ips/{network_label}'}], scope_types=['system', 'project']), policy.DocumentedRuleDefault(name=POLICY_ROOT % 'index', check_str=base.PROJECT_READER_OR_SYSTEM_READER, description='List IP addresses that are assigned to a server', operations=[{'method': 'GET', 'path': '/servers/{server_id}/ips'}], scope_types=['system', 'project'])]

@p.trace('list_rules')
def list_rules():
    return ips_policies