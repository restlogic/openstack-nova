from bees import profiler as p
from oslo_policy import policy
from nova.policies import base
POLICY_ROOT = 'os_compute_api:os-admin-actions:%s'
admin_actions_policies = [policy.DocumentedRuleDefault(name=POLICY_ROOT % 'reset_state', check_str=base.SYSTEM_ADMIN, description='Reset the state of a given server', operations=[{'method': 'POST', 'path': '/servers/{server_id}/action (os-resetState)'}], scope_types=['system', 'project']), policy.DocumentedRuleDefault(name=POLICY_ROOT % 'inject_network_info', check_str=base.SYSTEM_ADMIN, description='Inject network information into the server', operations=[{'method': 'POST', 'path': '/servers/{server_id}/action (injectNetworkInfo)'}], scope_types=['system', 'project'])]

@p.trace('list_rules')
def list_rules():
    return admin_actions_policies