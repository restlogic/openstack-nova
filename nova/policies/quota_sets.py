from bees import profiler as p
from oslo_policy import policy
from nova.policies import base
POLICY_ROOT = 'os_compute_api:os-quota-sets:%s'
quota_sets_policies = [policy.DocumentedRuleDefault(name=POLICY_ROOT % 'update', check_str=base.SYSTEM_ADMIN, description='Update the quotas', operations=[{'method': 'PUT', 'path': '/os-quota-sets/{tenant_id}'}], scope_types=['system']), policy.DocumentedRuleDefault(name=POLICY_ROOT % 'defaults', check_str=base.RULE_ANY, description='List default quotas', operations=[{'method': 'GET', 'path': '/os-quota-sets/{tenant_id}/defaults'}], scope_types=['system', 'project']), policy.DocumentedRuleDefault(name=POLICY_ROOT % 'show', check_str=base.PROJECT_READER_OR_SYSTEM_READER, description='Show a quota', operations=[{'method': 'GET', 'path': '/os-quota-sets/{tenant_id}'}], scope_types=['system', 'project']), policy.DocumentedRuleDefault(name=POLICY_ROOT % 'delete', check_str=base.SYSTEM_ADMIN, description='Revert quotas to defaults', operations=[{'method': 'DELETE', 'path': '/os-quota-sets/{tenant_id}'}], scope_types=['system']), policy.DocumentedRuleDefault(name=POLICY_ROOT % 'detail', check_str=base.PROJECT_READER_OR_SYSTEM_READER, description='Show the detail of quota', operations=[{'method': 'GET', 'path': '/os-quota-sets/{tenant_id}/detail'}], scope_types=['system', 'project'])]

@p.trace('list_rules')
def list_rules():
    return quota_sets_policies