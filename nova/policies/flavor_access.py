from bees import profiler as p
from oslo_policy import policy
from nova.policies import base
BASE_POLICY_NAME = 'os_compute_api:os-flavor-access'
POLICY_ROOT = 'os_compute_api:os-flavor-access:%s'
DEPRECATED_FLAVOR_ACCESS_POLICY = policy.DeprecatedRule(BASE_POLICY_NAME, base.RULE_ADMIN_OR_OWNER)
DEPRECATED_REASON = '\nNova API policies are introducing new default roles with scope_type\ncapabilities. Old policies are deprecated and silently going to be ignored\nin nova 23.0.0 release.\n'
flavor_access_policies = [policy.DocumentedRuleDefault(name=POLICY_ROOT % 'add_tenant_access', check_str=base.SYSTEM_ADMIN, description='Add flavor access to a tenant', operations=[{'method': 'POST', 'path': '/flavors/{flavor_id}/action (addTenantAccess)'}], scope_types=['system']), policy.DocumentedRuleDefault(name=POLICY_ROOT % 'remove_tenant_access', check_str=base.SYSTEM_ADMIN, description='Remove flavor access from a tenant', operations=[{'method': 'POST', 'path': '/flavors/{flavor_id}/action (removeTenantAccess)'}], scope_types=['system']), policy.DocumentedRuleDefault(name=BASE_POLICY_NAME, check_str=base.SYSTEM_READER, description='List flavor access information\n\nAllows access to the full list of tenants that have access\nto a flavor via an os-flavor-access API.\n', operations=[{'method': 'GET', 'path': '/flavors/{flavor_id}/os-flavor-access'}], scope_types=['system'], deprecated_rule=DEPRECATED_FLAVOR_ACCESS_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='21.0.0')]

@p.trace('list_rules')
def list_rules():
    return flavor_access_policies