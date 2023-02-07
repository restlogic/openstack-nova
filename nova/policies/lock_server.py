from bees import profiler as p
from oslo_policy import policy
from nova.policies import base
POLICY_ROOT = 'os_compute_api:os-lock-server:%s'
lock_server_policies = [policy.DocumentedRuleDefault(name=POLICY_ROOT % 'lock', check_str=base.PROJECT_MEMBER_OR_SYSTEM_ADMIN, description='Lock a server', operations=[{'path': '/servers/{server_id}/action (lock)', 'method': 'POST'}], scope_types=['system', 'project']), policy.DocumentedRuleDefault(name=POLICY_ROOT % 'unlock', check_str=base.PROJECT_MEMBER_OR_SYSTEM_ADMIN, description='Unlock a server', operations=[{'path': '/servers/{server_id}/action (unlock)', 'method': 'POST'}], scope_types=['system', 'project']), policy.DocumentedRuleDefault(name=POLICY_ROOT % 'unlock:unlock_override', check_str=base.SYSTEM_ADMIN, description='Unlock a server, regardless who locked the server.\n\nThis check is performed only after the check\nos_compute_api:os-lock-server:unlock passes', operations=[{'path': '/servers/{server_id}/action (unlock)', 'method': 'POST'}], scope_types=['system', 'project'])]

@p.trace('list_rules')
def list_rules():
    return lock_server_policies