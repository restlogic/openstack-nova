from bees import profiler as p
from oslo_policy import policy
from nova.policies import base
POLICY_ROOT = 'os_compute_api:os-suspend-server:%s'
suspend_server_policies = [policy.DocumentedRuleDefault(name=POLICY_ROOT % 'resume', check_str=base.PROJECT_MEMBER_OR_SYSTEM_ADMIN, description='Resume suspended server', operations=[{'method': 'POST', 'path': '/servers/{server_id}/action (resume)'}], scope_types=['system', 'project']), policy.DocumentedRuleDefault(name=POLICY_ROOT % 'suspend', check_str=base.PROJECT_MEMBER_OR_SYSTEM_ADMIN, description='Suspend server', operations=[{'method': 'POST', 'path': '/servers/{server_id}/action (suspend)'}], scope_types=['system', 'project'])]

@p.trace('list_rules')
def list_rules():
    return suspend_server_policies