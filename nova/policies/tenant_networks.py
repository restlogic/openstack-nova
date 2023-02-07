from bees import profiler as p
from oslo_policy import policy
from nova.policies import base
BASE_POLICY_NAME = 'os_compute_api:os-tenant-networks'
POLICY_NAME = 'os_compute_api:os-tenant-networks:%s'
DEPRECATED_POLICY = policy.DeprecatedRule(BASE_POLICY_NAME, base.RULE_ADMIN_OR_OWNER)
DEPRECATED_REASON = '\nNova API policies are introducing new default roles with scope_type\ncapabilities. Old policies are deprecated and silently going to be ignored\nin nova 23.0.0 release.\n'
tenant_networks_policies = [policy.DocumentedRuleDefault(name=POLICY_NAME % 'list', check_str=base.PROJECT_READER_OR_SYSTEM_READER, description='List project networks.\n\nThis API is proxy calls to the Network service. This is deprecated.', operations=[{'method': 'GET', 'path': '/os-tenant-networks'}], scope_types=['system', 'project'], deprecated_rule=DEPRECATED_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='22.0.0'), policy.DocumentedRuleDefault(name=POLICY_NAME % 'show', check_str=base.PROJECT_READER_OR_SYSTEM_READER, description='Show project network details.\n\nThis API is proxy calls to the Network service. This is deprecated.', operations=[{'method': 'GET', 'path': '/os-tenant-networks/{network_id}'}], scope_types=['system', 'project'], deprecated_rule=DEPRECATED_POLICY, deprecated_reason=DEPRECATED_REASON, deprecated_since='22.0.0')]

@p.trace('list_rules')
def list_rules():
    return tenant_networks_policies