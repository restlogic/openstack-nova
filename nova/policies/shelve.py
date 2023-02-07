from bees import profiler as p
from oslo_policy import policy
from nova.policies import base
POLICY_ROOT = 'os_compute_api:os-shelve:%s'
shelve_policies = [policy.DocumentedRuleDefault(name=POLICY_ROOT % 'shelve', check_str=base.PROJECT_MEMBER_OR_SYSTEM_ADMIN, description='Shelve server', operations=[{'method': 'POST', 'path': '/servers/{server_id}/action (shelve)'}], scope_types=['system', 'project']), policy.DocumentedRuleDefault(name=POLICY_ROOT % 'unshelve', check_str=base.PROJECT_MEMBER_OR_SYSTEM_ADMIN, description='Unshelve (restore) shelved server', operations=[{'method': 'POST', 'path': '/servers/{server_id}/action (unshelve)'}], scope_types=['system', 'project']), policy.DocumentedRuleDefault(name=POLICY_ROOT % 'shelve_offload', check_str=base.SYSTEM_ADMIN, description='Shelf-offload (remove) server', operations=[{'method': 'POST', 'path': '/servers/{server_id}/action (shelveOffload)'}], scope_types=['system', 'project'])]

@p.trace('list_rules')
def list_rules():
    return shelve_policies