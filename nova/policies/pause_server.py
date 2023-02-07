from bees import profiler as p
from oslo_policy import policy
from nova.policies import base
POLICY_ROOT = 'os_compute_api:os-pause-server:%s'
pause_server_policies = [policy.DocumentedRuleDefault(name=POLICY_ROOT % 'pause', check_str=base.PROJECT_MEMBER_OR_SYSTEM_ADMIN, description='Pause a server', operations=[{'path': '/servers/{server_id}/action (pause)', 'method': 'POST'}], scope_types=['system', 'project']), policy.DocumentedRuleDefault(name=POLICY_ROOT % 'unpause', check_str=base.PROJECT_MEMBER_OR_SYSTEM_ADMIN, description='Unpause a paused server', operations=[{'path': '/servers/{server_id}/action (unpause)', 'method': 'POST'}], scope_types=['system', 'project'])]

@p.trace('list_rules')
def list_rules():
    return pause_server_policies